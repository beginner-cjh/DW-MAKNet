﻿import re
import time
import math
import numpy as np
from functools import partial
from typing import Optional, Union, Type, List, Tuple, Callable, Dict

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as checkpoint
from einops import rearrange, repeat
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from mamba_ssm.ops.selective_scan_interface import selective_scan_fn, selective_scan_ref

DropPath.__repr__ = lambda self: f"timm.DropPath({self.drop_prob})"

from nnunetv2.utilities.plans_handling.plans_handler import ConfigurationManager, PlansManager
from monai.networks.blocks.dynunet_block import UnetOutBlock
from monai.networks.blocks.unetr_block import UnetrBasicBlock, UnetrUpBlock
from dynamic_network_architectures.initialization.weight_init import init_last_bn_before_add_to_0
from nnunetv2.utilities.network_initialization import InitWeights_He


class PatchEmbed2D(nn.Module):
    r""" Image to Patch Embedding
    Args:
        patch_size (int): Patch token size. Default: 4.
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        norm_layer (nn.Module, optional): Normalization layer. Default: None
    """

    def __init__(self, img_size=64, patch_size=2, in_chans=3, embed_dim=96, norm_layer=None, **kwargs):
        super().__init__()
        if isinstance(patch_size, int):
            patch_size = (patch_size, patch_size)
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size,
                              stride=patch_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

        patches_resolution = img_size // patch_size[0]
        self.patches_resolution = patches_resolution  # This doesn't work.

    def forward(self, x):

        x = self.proj(x)
        B, C, H, W = x.shape

        x = x.permute(0, 2, 3, 1)
        if self.norm is not None:
            x = self.norm(x)

        x = x.view(B, -1, C)
        return x, H, W


class PatchMerging2D(nn.Module):
    r""" Patch Merging Layer.##(B,H,W,C)
    Args:
        input_resolution (tuple[int]): Resolution of input feature.
        dim (int): Number of input channels.
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.reduction = nn.Linear(4 * dim, 2 * dim, bias=False)
        self.norm = norm_layer(4 * dim)

    def forward(self, x):

        B, H, W, C = x.shape
        SHAPE_FIX = [-1, -1]
        if (W % 2 != 0) or (H % 2 != 0):
            print(f"Warning, x.shape {x.shape} is not match even ===========", flush=True)
            SHAPE_FIX[0] = H // 2
            SHAPE_FIX[1] = W // 2

        x0 = x[:, 0::2, 0::2, :]
        x1 = x[:, 1::2, 0::2, :]
        x2 = x[:, 0::2, 1::2, :]
        x3 = x[:, 1::2, 1::2, :]

        if SHAPE_FIX[0] > 0:
            x0 = x0[:, :SHAPE_FIX[0], :SHAPE_FIX[1], :]
            x1 = x1[:, :SHAPE_FIX[0], :SHAPE_FIX[1], :]
            x2 = x2[:, :SHAPE_FIX[0], :SHAPE_FIX[1], :]
            x3 = x3[:, :SHAPE_FIX[0], :SHAPE_FIX[1], :]

        x = torch.cat([x0, x1, x2, x3], -1)
        x = x.view(B, H // 2, W // 2, 4 * C)

        x = self.norm(x)
        x = self.reduction(x)

        return x


class PatchExpand(nn.Module):
    """
    Reference: https://arxiv.org/pdf/2105.05537.pdf
    """

    def __init__(self, input_resolution, dim, dim_scale=2, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.expand = nn.Linear(dim, 2 * dim, bias=False) if dim_scale == 2 else nn.Identity()
        self.norm = norm_layer(dim // dim_scale)

    def forward(self, x):
        x = x.permute(0, 2, 3, 1)  # B, C, H, W ==> B, H, W, C
        x = self.expand(x)
        B, H, W, C = x.shape

        x = x.view(B, H, W, C)
        x = rearrange(x, 'b h w (p1 p2 c)-> b (h p1) (w p2) c', p1=2, p2=2, c=C // 4)
        x = x.view(B, -1, C // 4)
        x = self.norm(x)
        x = x.reshape(B, H * 2, W * 2, C // 4)

        return x


class EFConv(nn.Module):
    r""" Expanded  field convolution
        Args:
            kernel_size (int): Convolution kernel size for convolution. Default: 3.
            stride (int): Step size of the convolution. Default:1.
            in_chans (int): Number of input image channels. Default: 192.
            embed_dim (int): Number of linear projection output channels. Default: 192.
        """
    def __init__(self,  kernel_size=3, stride=1, in_chans=192, embed_dim=192):
        super().__init__()

        kernel_size = to_2tuple(kernel_size)

        self.proj1 = nn.Conv2d(in_chans, embed_dim, kernel_size=kernel_size, stride=stride,
                              padding=(kernel_size[0] // 2, kernel_size[1] // 2))
        self.proj2 = nn.Conv2d(in_chans, embed_dim, kernel_size=kernel_size, stride=stride,
                              padding=(kernel_size[0] // 2, kernel_size[1] // 2))
        self.norm = nn.LayerNorm(embed_dim)

    def forward(self, x):
        x = self.proj1(x)
        x = self.proj2(x)
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)

        return x, H, W


class ConvLayer(nn.Module):
    r""" Conv Block for encoder """

    def __init__(self, in_ch, out_ch):
        super(ConvLayer, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True)
        )

    def forward(self, input):
        return self.conv(input)


class D_ConvLayer(nn.Module):
    r""" Conv Block for decoder """

    def __init__(self, dim):
        super(D_ConvLayer, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(dim, dim, 3, padding=1, bias=False),
            nn.BatchNorm2d(dim),
            nn.ReLU(inplace=True),
            nn.Conv2d(dim, dim, 3, padding=1, bias=False),
            nn.BatchNorm2d(dim),
            nn.ReLU(inplace=True)
        )

    def forward(self, input):
        return self.conv(input) + input


class DW_bn_relu(nn.Module):
    r""" DWConv Block for KANLayer """
    def __init__(self, dim=768):
        super(DW_bn_relu, self).__init__()
        self.dwconv = nn.Conv2d(dim, dim, 3, 1, 1, bias=True, groups=dim)
        self.bn = nn.BatchNorm2d(dim)
        self.relu = nn.ReLU()

    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.transpose(1, 2).view(B, C, H, W)
        x = self.dwconv(x)
        x = self.bn(x)
        x = self.relu(x)
        x = x.flatten(2).transpose(1, 2)

        return x


class KANLinear(torch.nn.Module):
    r""" Core modules of KAN
        Reference: https://arxiv.org/abs/2404.19756
        """
    def __init__(
        self,
        in_features,
        out_features,
        grid_size=5,
        spline_order=3,
        scale_noise=0.1,
        scale_base=1.0,
        scale_spline=1.0,
        enable_standalone_scale_spline=True,
        base_activation=torch.nn.SiLU,
        grid_eps=0.02,
        grid_range=[-1, 1],
    ):
        super(KANLinear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.grid_size = grid_size
        self.spline_order = spline_order

        h = (grid_range[1] - grid_range[0]) / grid_size
        grid = (
            (
                torch.arange(-spline_order, grid_size + spline_order + 1) * h
                + grid_range[0]
            )
            .expand(in_features, -1)
            .contiguous()
        )
        self.register_buffer("grid", grid)

        self.base_weight = torch.nn.Parameter(torch.Tensor(out_features, in_features))
        self.spline_weight = torch.nn.Parameter(
            torch.Tensor(out_features, in_features, grid_size + spline_order)
        )
        if enable_standalone_scale_spline:
            self.spline_scaler = torch.nn.Parameter(
                torch.Tensor(out_features, in_features)
            )

        self.scale_noise = scale_noise
        self.scale_base = scale_base
        self.scale_spline = scale_spline
        self.enable_standalone_scale_spline = enable_standalone_scale_spline
        self.base_activation = base_activation()
        self.grid_eps = grid_eps

        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.kaiming_uniform_(self.base_weight, a=math.sqrt(5) * self.scale_base)
        with torch.no_grad():
            noise = (
                (
                    torch.rand(self.grid_size + 1, self.in_features, self.out_features)
                    - 1 / 2
                )
                * self.scale_noise
                / self.grid_size
            )
            self.spline_weight.data.copy_(
                (self.scale_spline if not self.enable_standalone_scale_spline else 1.0)
                * self.curve2coeff(
                    self.grid.T[self.spline_order : -self.spline_order],
                    noise,
                )
            )
            if self.enable_standalone_scale_spline:
                # torch.nn.init.constant_(self.spline_scaler, self.scale_spline)
                torch.nn.init.kaiming_uniform_(self.spline_scaler, a=math.sqrt(5) * self.scale_spline)

    def b_splines(self, x: torch.Tensor):
        """
        Compute the B-spline bases for the given input tensor.

        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, in_features).

        Returns:
            torch.Tensor: B-spline bases tensor of shape (batch_size, in_features, grid_size + spline_order).
        """
        assert x.dim() == 2 and x.size(1) == self.in_features

        grid: torch.Tensor = (
            self.grid
        )  # (in_features, grid_size + 2 * spline_order + 1)
        x = x.unsqueeze(-1)
        bases = ((x >= grid[:, :-1]) & (x < grid[:, 1:])).to(x.dtype)
        for k in range(1, self.spline_order + 1):
            bases = (
                (x - grid[:, : -(k + 1)])
                / (grid[:, k:-1] - grid[:, : -(k + 1)])
                * bases[:, :, :-1]
            ) + (
                (grid[:, k + 1 :] - x)
                / (grid[:, k + 1 :] - grid[:, 1:(-k)])
                * bases[:, :, 1:]
            )

        assert bases.size() == (
            x.size(0),
            self.in_features,
            self.grid_size + self.spline_order,
        )
        return bases.contiguous()

    def curve2coeff(self, x: torch.Tensor, y: torch.Tensor):
        """
        Compute the coefficients of the curve that interpolates the given points.

        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, in_features).
            y (torch.Tensor): Output tensor of shape (batch_size, in_features, out_features).

        Returns:
            torch.Tensor: Coefficients tensor of shape (out_features, in_features, grid_size + spline_order).
        """
        assert x.dim() == 2 and x.size(1) == self.in_features
        assert y.size() == (x.size(0), self.in_features, self.out_features)

        A = self.b_splines(x).transpose(
            0, 1
        )  # (in_features, batch_size, grid_size + spline_order)
        B = y.transpose(0, 1)  # (in_features, batch_size, out_features)
        solution = torch.linalg.lstsq(
            A, B
        ).solution  # (in_features, grid_size + spline_order, out_features)
        result = solution.permute(
            2, 0, 1
        )  # (out_features, in_features, grid_size + spline_order)

        assert result.size() == (
            self.out_features,
            self.in_features,
            self.grid_size + self.spline_order,
        )
        return result.contiguous()

    @property
    def scaled_spline_weight(self):
        return self.spline_weight * (
            self.spline_scaler.unsqueeze(-1)
            if self.enable_standalone_scale_spline
            else 1.0
        )

    def forward(self, x: torch.Tensor):
        assert x.dim() == 2 and x.size(1) == self.in_features

        base_output = F.linear(self.base_activation(x), self.base_weight)
        spline_output = F.linear(
            self.b_splines(x).view(x.size(0), -1),
            self.scaled_spline_weight.view(self.out_features, -1),
        )
        return base_output + spline_output

    @torch.no_grad()
    def update_grid(self, x: torch.Tensor, margin=0.01):
        assert x.dim() == 2 and x.size(1) == self.in_features
        batch = x.size(0)

        splines = self.b_splines(x)  # (batch, in, coeff)
        splines = splines.permute(1, 0, 2)  # (in, batch, coeff)
        orig_coeff = self.scaled_spline_weight  # (out, in, coeff)
        orig_coeff = orig_coeff.permute(1, 2, 0)  # (in, coeff, out)
        unreduced_spline_output = torch.bmm(splines, orig_coeff)  # (in, batch, out)
        unreduced_spline_output = unreduced_spline_output.permute(
            1, 0, 2
        )  # (batch, in, out)

        # sort each channel individually to collect data distribution
        x_sorted = torch.sort(x, dim=0)[0]
        grid_adaptive = x_sorted[
            torch.linspace(
                0, batch - 1, self.grid_size + 1, dtype=torch.int64, device=x.device
            )
        ]

        uniform_step = (x_sorted[-1] - x_sorted[0] + 2 * margin) / self.grid_size
        grid_uniform = (
            torch.arange(
                self.grid_size + 1, dtype=torch.float32, device=x.device
            ).unsqueeze(1)
            * uniform_step
            + x_sorted[0]
            - margin
        )

        grid = self.grid_eps * grid_uniform + (1 - self.grid_eps) * grid_adaptive
        grid = torch.concatenate(
            [
                grid[:1]
                - uniform_step
                * torch.arange(self.spline_order, 0, -1, device=x.device).unsqueeze(1),
                grid,
                grid[-1:]
                + uniform_step
                * torch.arange(1, self.spline_order + 1, device=x.device).unsqueeze(1),
            ],
            dim=0,
        )

        self.grid.copy_(grid.T)
        self.spline_weight.data.copy_(self.curve2coeff(x, unreduced_spline_output))

    def regularization_loss(self, regularize_activation=1.0, regularize_entropy=1.0):
        """
        Compute the regularization loss.

        This is a dumb simulation of the original L1 regularization as stated in the
        paper, since the original one requires computing absolutes and entropy from the
        expanded (batch, in_features, out_features) intermediate tensor, which is hidden
        behind the F.linear function if we want an memory efficient implementation.

        The L1 regularization is now computed as mean absolute value of the spline
        weights. The authors implementation also includes this term in addition to the
        sample-based regularization.
        """
        l1_fake = self.spline_weight.abs().mean(-1)
        regularization_loss_activation = l1_fake.sum()
        p = l1_fake / regularization_loss_activation
        regularization_loss_entropy = -torch.sum(p * p.log())
        return (
            regularize_activation * regularization_loss_activation
            + regularize_entropy * regularization_loss_entropy
        )


class KANLayer(nn.Module):
    r"""
        Reference: https://arxiv.org/pdf/2406.02918
        """
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=torch.nn.SiLU, drop=0., no_kan=False):
        super().__init__()
        self.out_features = out_features
        self.hidden_features = hidden_features
        self.dim = in_features

        grid_size = 3
        spline_order = 3
        scale_noise = 0.1
        scale_base = 1.0
        scale_spline = 1.0
        base_activation = act_layer
        grid_eps = 0.02
        grid_range = [-1, 1]

        if not no_kan:
            self.fc1 = KANLinear(
                in_features,
                hidden_features,
                grid_size=grid_size,
                spline_order=spline_order,
                scale_noise=scale_noise,
                scale_base=scale_base,
                scale_spline=scale_spline,
                base_activation=base_activation,
                grid_eps=grid_eps,
                grid_range=grid_range,
            )
            self.fc2 = KANLinear(
                hidden_features,
                out_features,
                grid_size=grid_size,
                spline_order=spline_order,
                scale_noise=scale_noise,
                scale_base=scale_base,
                scale_spline=scale_spline,
                base_activation=base_activation,
                grid_eps=grid_eps,
                grid_range=grid_range,
            )

        else:
            self.fc1 = nn.Linear(in_features, hidden_features)
            self.fc2 = nn.Linear(hidden_features, out_features)

        self.dwconv_1 = DW_bn_relu(hidden_features)
        self.dwconv_2 = DW_bn_relu(out_features)

        self.drop = nn.Dropout(drop)

    def forward(self, x, H, W):

        B, N, C = x.shape

        x1 = self.fc1(x.reshape(B * N, C))
        x1 = x1.reshape(B, N, self.hidden_features).contiguous()
        x1 = self.dwconv_1(x1, H, W)

        x1 = self.fc2(x1.reshape(B * N, self.hidden_features))
        x1 = x1.reshape(B, N, self.out_features).contiguous()
        x1 = self.dwconv_2(x1, H, W)

        x = self.drop(x + x1)

        return x


class KANBlock(nn.Module):
    r"""KAN Block
        Args:
            dim (int): Number of input channels.
            depth (int): Number of blocks.
            drop (float, optional): dropout rate. Default: 0.0
            drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
            norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
            act_layer (nn.Module, optional): Basic activation. Default: nn.SiLU
        """
    def __init__(self, dim, drop=0., drop_path=0., act_layer=nn.SiLU, norm_layer=nn.LayerNorm, no_kan=False):
        super().__init__()

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm = norm_layer(dim)
        self.layer = KANLayer(in_features=dim, hidden_features=dim//8,out_features=dim, act_layer=act_layer, drop=drop,
                              no_kan=no_kan)


    def forward(self, x, H, W):

        B, _, C = x.shape
        xr = self.norm(x)
        xr = self.layer(xr, H, W)
        xr = x + self.drop_path(xr)

        return xr


class W_SS2D(nn.Module):  # SS2D Block

    def __init__(
            self,
            d_model,
            d_state=16,
            d_conv=3,
            expand=2,
            dt_rank="auto",
            dt_min=0.001,
            dt_max=0.1,
            dt_init="random",
            dt_scale=1.0,
            dt_init_floor=1e-4,
            dropout=0.,
            conv_bias=True,
            bias=False,
            device=None,
            dtype=None,
            **kwargs,
    ):
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__()
        self.d_model = d_model
        self.d_state = d_state
        self.d_conv = d_conv
        self.expand = expand
        self.d_inner = int(self.expand * self.d_model)
        self.dt_rank = math.ceil(self.d_model / 16) if dt_rank == "auto" else dt_rank

        self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=bias, **factory_kwargs)

        self.conv2d = nn.Conv2d(
            in_channels=self.d_inner,
            out_channels=self.d_inner,
            groups=self.d_inner,
            bias=conv_bias,
            kernel_size=d_conv,
            padding=(d_conv - 1) // 2,
            **factory_kwargs,
        )
        self.act = nn.SiLU()

        self.x_proj = (
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
        )
        self.x_proj_weight = nn.Parameter(
            torch.stack([t.weight for t in self.x_proj], dim=0))  # (K=4, N, inner)
        del self.x_proj

        self.dt_projs = (
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
        )
        self.dt_projs_weight = nn.Parameter(
            torch.stack([t.weight for t in self.dt_projs], dim=0))  # (K=4, inner, rank)
        self.dt_projs_bias = nn.Parameter(
            torch.stack([t.bias for t in self.dt_projs], dim=0))  # (K=4, inner)
        del self.dt_projs

        self.A_logs = self.A_log_init(self.d_state, self.d_inner, copies=4, merge=True)  # (K=4, D, N)
        self.Ds = self.D_init(self.d_inner, copies=4, merge=True)  # (K=4, D, N)

        self.selective_scan = selective_scan_fn

        self.out_norm = nn.LayerNorm(self.d_inner)
        self.out_proj = nn.Linear(self.d_inner, self.d_model, bias=bias, **factory_kwargs)
        self.dropout = nn.Dropout(dropout) if dropout > 0. else None

    @staticmethod
    def dt_init(dt_rank, d_inner, dt_scale=1.0, dt_init="random", dt_min=0.001, dt_max=0.1, dt_init_floor=1e-4,
                **factory_kwargs):
        dt_proj = nn.Linear(dt_rank, d_inner, bias=True, **factory_kwargs)

        # Initialize special dt projection to preserve variance at initialization
        dt_init_std = dt_rank ** -0.5 * dt_scale
        if dt_init == "constant":
            nn.init.constant_(dt_proj.weight, dt_init_std)
        elif dt_init == "random":
            nn.init.uniform_(dt_proj.weight, -dt_init_std, dt_init_std)
        else:
            raise NotImplementedError

        # Initialize dt bias so that F.softplus(dt_bias) is between dt_min and dt_max
        dt = torch.exp(
            torch.rand(d_inner, **factory_kwargs) * (math.log(dt_max) - math.log(dt_min))
            + math.log(dt_min)
        ).clamp(min=dt_init_floor)
        # Inverse of softplus: https://github.com/pytorch/pytorch/issues/72759
        inv_dt = dt + torch.log(-torch.expm1(-dt))
        with torch.no_grad():
            dt_proj.bias.copy_(inv_dt)
        # Our initialization would set all Linear.bias to zero, need to mark this one as _no_reinit
        dt_proj.bias._no_reinit = True

        return dt_proj

    @staticmethod
    def A_log_init(d_state, d_inner, copies=1, device=None, merge=True):
        # S4D real initialization
        A = repeat(
            torch.arange(1, d_state + 1, dtype=torch.float32, device=device),
            "n -> d n",
            d=d_inner,
        ).contiguous()
        A_log = torch.log(A)  # Keep A_log in fp32
        if copies > 1:
            A_log = repeat(A_log, "d n -> r d n", r=copies)
            if merge:
                A_log = A_log.flatten(0, 1)
        A_log = nn.Parameter(A_log)
        A_log._no_weight_decay = True
        return A_log

    @staticmethod
    def D_init(d_inner, copies=1, device=None, merge=True):
        # D "skip" parameter
        D = torch.ones(d_inner, device=device)
        if copies > 1:
            D = repeat(D, "n1 -> r n1", r=copies)
            if merge:
                D = D.flatten(0, 1)
        D = nn.Parameter(D)  # Keep in fp32
        D._no_weight_decay = True
        return D

    def forward_core(self, x: torch.Tensor):  # SS2D
        B, C, H, W = x.shape
        kernel = H // 2
        assert H % kernel == 0 and W % kernel == 0, "H 和 W 必须能被 kernel 整除"
        L = H * W
        K = 4

        # 分块并调整维度顺序
        x = x.view(B, C, H // kernel, kernel, W // kernel, kernel)
        x = x.permute(0, 1, 3, 5, 2, 4)  # [B, C, kernel, kernel, H//kernel, W//kernel]

        x = torch.stack([x.reshape(B, -1, L), x.permute(0, 1, 3, 2, 5, 4).reshape(B, -1, L)],
                        dim=1).view(B, 2, -1, L)
        xs = torch.cat([x, torch.flip(x, dims=[-1])], dim=1)  # (b,k,d,l)

        x_dbl = torch.einsum("b k d l, k c d -> b k c l", xs.view(B, K, -1, L), self.x_proj_weight)
        dts, Bs, Cs = torch.split(x_dbl, [self.dt_rank, self.d_state, self.d_state], dim=2)
        dts = torch.einsum("b k r l, k d r -> b k d l", dts.view(B, K, -1, L), self.dt_projs_weight)

        xs = xs.float().view(B, -1, L)  # (b, k * d, l)
        dts = dts.contiguous().float().view(B, -1, L)  # (b, k * d, l)
        Bs = Bs.float().view(B, K, -1, L)  # (b, k, d_state, l)
        Cs = Cs.float().view(B, K, -1, L)  # (b, k, d_state, l)
        Ds = self.Ds.float().view(-1)  # (k * d)
        As = -torch.exp(self.A_logs.float()).view(-1, self.d_state)  # (k * d, d_state)
        dt_projs_bias = self.dt_projs_bias.float().view(-1)  # (k * d)

        out_y = self.selective_scan(  # S6 block
            xs, dts,
            As, Bs, Cs, Ds, z=None,
            delta_bias=dt_projs_bias,
            delta_softplus=True,
            return_last_state=False,
        ).view(B, K, -1, L)
        assert out_y.dtype == torch.float

        lr_y = out_y[:, 0, :, :].reshape(B, -1, L)
        ud_y = out_y[:, 1, :, :].reshape(B, -1, kernel, kernel, H // kernel, W // kernel).permute(0, 1, 3, 2, 5,
                                                                                                  4).reshape(B, -1, L)
        rl_y = torch.flip(out_y[:, 2, :, :], dims=[-1]).reshape(B, -1, L)
        du_y = torch.flip(out_y[:, 3, :, :], dims=[-1]).reshape(B, -1, kernel, kernel, H // kernel,
                                                                W // kernel).permute(0, 1, 3, 2, 5, 4).reshape(B, -1, L)

        # inv_y = torch.flip(out_y[:, 2:4], dims=[-1]).view(B, 2, -1, L)
        # wh_y = torch.transpose(out_y[:, 1].view(B, -1, W, H), dim0=2, dim1=3).contiguous().view(B, -1, L)
        # invwh_y = torch.transpose(inv_y[:, 1].view(B, -1, W, H), dim0=2, dim1=3).contiguous().view(B, -1, L)

        return lr_y, ud_y, rl_y, du_y

    def forward(self, x: torch.Tensor, **kwargs):  # SS2D block
        B, H, W, C = x.shape
        kernel = H // 2

        xz = self.in_proj(x)
        x, z = xz.chunk(2, dim=-1)  # (b, h, w, d)

        x = x.permute(0, 3, 1, 2).contiguous()  # （b, d, h, w）
        x = self.act(self.conv2d(x))  # (b, d, h, w)
        y1, y2, y3, y4 = self.forward_core(x)
        assert y1.dtype == torch.float32
        y = y1 + y2 + y3 + y4
        y = y.reshape(B, -1, kernel, kernel, H // kernel, W // kernel).permute(0, 1, 4, 2, 5, 3).contiguous().view(B,
                                                                                                                   -1,
                                                                                                                   H, W)
        y = y.permute(0, 2, 3, 1)
        y = self.out_norm(y)
        y = y * F.silu(z)
        out = self.out_proj(y)
        if self.dropout is not None:
            out = self.dropout(out)
        return out


class C_SS2D(nn.Module):  # SS2D Block
    def __init__(
            self,
            d_model,
            d_state=16,
            d_conv=3,
            expand=2,
            dt_rank="auto",
            dt_min=0.001,
            dt_max=0.1,
            dt_init="random",
            dt_scale=1.0,
            dt_init_floor=1e-4,
            dropout=0.,
            conv_bias=True,
            bias=False,
            device=None,
            dtype=None,
            **kwargs,
    ):
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__()
        self.d_model = d_model
        self.d_state = d_state
        self.d_conv = d_conv
        self.expand = expand
        self.d_inner = int(self.expand * self.d_model)
        self.dt_rank = math.ceil(self.d_model / 16) if dt_rank == "auto" else dt_rank

        self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=bias, **factory_kwargs)

        self.conv2d = nn.Conv2d(
            in_channels=self.d_inner,
            out_channels=self.d_inner,
            groups=self.d_inner,
            bias=conv_bias,
            kernel_size=d_conv,
            padding=(d_conv - 1) // 2,
            **factory_kwargs,
        )
        self.act = nn.SiLU()

        self.x_proj = (
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
            nn.Linear(self.d_inner, (self.dt_rank + self.d_state * 2), bias=False, **factory_kwargs),
        )
        self.x_proj_weight = nn.Parameter(
            torch.stack([t.weight for t in self.x_proj], dim=0))  # (K=4, N, inner)
        del self.x_proj

        self.dt_projs = (
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
            self.dt_init(self.dt_rank, self.d_inner, dt_scale, dt_init, dt_min, dt_max, dt_init_floor,
                         **factory_kwargs),
        )
        self.dt_projs_weight = nn.Parameter(
            torch.stack([t.weight for t in self.dt_projs], dim=0))  # (K=4, inner, rank)
        self.dt_projs_bias = nn.Parameter(
            torch.stack([t.bias for t in self.dt_projs], dim=0))  # (K=4, inner)
        del self.dt_projs

        self.A_logs = self.A_log_init(self.d_state, self.d_inner, copies=4, merge=True)  # (K=4, D, N)
        self.Ds = self.D_init(self.d_inner, copies=4, merge=True)  # (K=4, D, N)

        self.selective_scan = selective_scan_fn

        self.out_norm = nn.LayerNorm(self.d_inner)
        self.out_proj = nn.Linear(self.d_inner, self.d_model, bias=bias, **factory_kwargs)
        self.dropout = nn.Dropout(dropout) if dropout > 0. else None

    @staticmethod
    def dt_init(dt_rank, d_inner, dt_scale=1.0, dt_init="random", dt_min=0.001, dt_max=0.1, dt_init_floor=1e-4,
                **factory_kwargs):
        dt_proj = nn.Linear(dt_rank, d_inner, bias=True, **factory_kwargs)

        # Initialize special dt projection to preserve variance at initialization
        dt_init_std = dt_rank ** -0.5 * dt_scale
        if dt_init == "constant":
            nn.init.constant_(dt_proj.weight, dt_init_std)
        elif dt_init == "random":
            nn.init.uniform_(dt_proj.weight, -dt_init_std, dt_init_std)
        else:
            raise NotImplementedError

        # Initialize dt bias so that F.softplus(dt_bias) is between dt_min and dt_max
        dt = torch.exp(
            torch.rand(d_inner, **factory_kwargs) * (math.log(dt_max) - math.log(dt_min))
            + math.log(dt_min)
        ).clamp(min=dt_init_floor)
        # Inverse of softplus: https://github.com/pytorch/pytorch/issues/72759
        inv_dt = dt + torch.log(-torch.expm1(-dt))
        with torch.no_grad():
            dt_proj.bias.copy_(inv_dt)
        # Our initialization would set all Linear.bias to zero, need to mark this one as _no_reinit
        dt_proj.bias._no_reinit = True

        return dt_proj

    @staticmethod
    def A_log_init(d_state, d_inner, copies=1, device=None, merge=True):
        # S4D real initialization
        A = repeat(
            torch.arange(1, d_state + 1, dtype=torch.float32, device=device),
            "n -> d n",
            d=d_inner,
        ).contiguous()
        A_log = torch.log(A)  # Keep A_log in fp32
        if copies > 1:
            A_log = repeat(A_log, "d n -> r d n", r=copies)
            if merge:
                A_log = A_log.flatten(0, 1)
        A_log = nn.Parameter(A_log)
        A_log._no_weight_decay = True
        return A_log

    @staticmethod
    def D_init(d_inner, copies=1, device=None, merge=True):
        # D "skip" parameter
        D = torch.ones(d_inner, device=device)
        if copies > 1:
            D = repeat(D, "n1 -> r n1", r=copies)
            if merge:
                D = D.flatten(0, 1)
        D = nn.Parameter(D)  # Keep in fp32
        D._no_weight_decay = True
        return D

    def forward_core(self, x: torch.Tensor):  # SS2D
        B, C, H, W = x.shape
        L = H * W
        K = 4

        # 分块并调整维度顺序
        xs = rearrange_center_blocks(x, H // 2)

        x_dbl = torch.einsum("b k d l, k c d -> b k c l", xs.view(B, K, -1, L), self.x_proj_weight)
        dts, Bs, Cs = torch.split(x_dbl, [self.dt_rank, self.d_state, self.d_state], dim=2)
        dts = torch.einsum("b k r l, k d r -> b k d l", dts.view(B, K, -1, L), self.dt_projs_weight)

        xs = xs.float().view(B, -1, L)  # (b, k * d, l)
        dts = dts.contiguous().float().view(B, -1, L)  # (b, k * d, l)
        Bs = Bs.float().view(B, K, -1, L)  # (b, k, d_state, l)
        Cs = Cs.float().view(B, K, -1, L)  # (b, k, d_state, l)
        Ds = self.Ds.float().view(-1)  # (k * d)
        As = -torch.exp(self.A_logs.float()).view(-1, self.d_state)  # (k * d, d_state)
        dt_projs_bias = self.dt_projs_bias.float().view(-1)  # (k * d)

        out_y = self.selective_scan(  # S6 block
            xs, dts,
            As, Bs, Cs, Ds, z=None,
            delta_bias=dt_projs_bias,
            delta_softplus=True,
            return_last_state=False,
        ).view(B, K, -1, L)
        assert out_y.dtype == torch.float

        return out_y

    def forward(self, x: torch.Tensor, **kwargs):  # SS2D block
        B, H, W, C = x.shape
        kernel = H // 2

        xz = self.in_proj(x)
        x, z = xz.chunk(2, dim=-1)  # (b, h, w, d)

        x = x.permute(0, 3, 1, 2).contiguous()  # （b, d, h, w）
        x = self.act(self.conv2d(x))  # (b, d, h, w)
        out = self.forward_core(x)
        y = recover_center_blocks(out, kernel=kernel)
        assert y.dtype == torch.float32
        y = y.permute(0, 2, 3, 1)
        y = self.out_norm(y)
        y = y * F.silu(z)
        out = self.out_proj(y)
        if self.dropout is not None:
            out = self.dropout(out)
        return out


def rearrange_center_blocks(x: torch.Tensor, kernel: int) -> torch.Tensor:
    """
    args:
    x: input，shape (B, H, W, C)
    kernel: window size
    output_shape (B, H*W, C)
    """
    B, C, H, W = x.shape
    h_center = (H - kernel) // 2
    w_center = (W - kernel) // 2
    L = H * W

    blocks_lr = []
    blocks_ud = []

    if h_center > 0 and w_center > 0:
        top_left = x[:, :, :h_center, :w_center]
        blocks_lr.append(top_left)
    if h_center > 0:
        top = x[:, :, :h_center, w_center:w_center + kernel]
        blocks_lr.append(top)
    if h_center > 0 and (W - (w_center + kernel)) > 0:
        top_right = x[:, :, :h_center, w_center + kernel:]
        blocks_lr.append(top_right)
    if w_center > 0:
        left = x[:, :, h_center:h_center + kernel, :w_center]
        blocks_lr.append(left)
    center_block = x[:, :, h_center:h_center + kernel, w_center:w_center + kernel]
    blocks_lr.append(center_block)
    if (W - (w_center + kernel)) > 0:
        right = x[:, :, h_center:h_center + kernel, w_center + kernel:]
        blocks_lr.append(right)
    if (H - (h_center + kernel)) > 0 and w_center > 0:
        bottom_left = x[:, :, h_center + kernel:, :w_center]
        blocks_lr.append(bottom_left)
    if (H - (h_center + kernel)) > 0:
        bottom = x[:, :, h_center + kernel:, w_center:w_center + kernel]
        blocks_lr.append(bottom)
    if (H - (h_center + kernel)) > 0 and (W - (w_center + kernel)) > 0:
        bottom_right = x[:, :, h_center + kernel:, w_center + kernel:]
        blocks_lr.append(bottom_right)

    blocks_ud.append(top_left)
    blocks_ud.append(left)
    blocks_ud.append(bottom_left)
    blocks_ud.append(top)
    blocks_ud.append(center_block)
    blocks_ud.append(bottom)
    blocks_ud.append(top_right)
    blocks_ud.append(right)
    blocks_ud.append(bottom_right)

    flattened = []
    for block in blocks_lr:
        flattened.append(block.reshape(B, C, -1))
    output1 = torch.cat(flattened, dim=-1)  # (B, C, total_L) #left to right

    for i, block in enumerate(blocks_ud):
        flattened[i] = block.transpose(2, 3).contiguous().view(B, C, -1)
    output2 = torch.cat(flattened, dim=-1)  # top to bottom

    output3 = torch.flip(output1,dims=[-1])  # right to left
    output4 = torch.flip(output2, dims=[-1])  # bottom to top

    output = torch.stack([output1, output2, output3, output4], dim=1).view(B, 4, -1, L)

    return output


def recover_window_lr(x: torch.Tensor, kernel: int) -> torch.Tensor:
    # in:B,C,L , out:B,C,H,W
    B, C, L = x.shape
    short = kernel // 2
    short1 = kernel // 2 + 1
    small_l = short * short
    small_l1 = short * short1
    medium_l = short * short * 2
    medium_l1 = short * 2 * short1
    high_l = short * short * 4

    if kernel / 2 == kernel // 2:
        top_left = x[:, :, :small_l].reshape(B, C, short, short)
        top = x[:, :, small_l:small_l + medium_l].reshape(B, C, short, short * 2)
        top_right = x[:, :, small_l + medium_l:2 * small_l + medium_l].reshape(B, C, short, short)
        left = x[:, :, 2 * small_l + medium_l:2 * small_l + 2 * medium_l].reshape(B, C, short * 2, short)
        center_block = x[:, :, 2 * small_l + 2 * medium_l:2 * small_l + 2 * medium_l + high_l].reshape(B, C, short * 2,
                                                                                                       short * 2)
        right = x[:, :, 2 * small_l + 2 * medium_l + high_l:2 * small_l + 3 * medium_l + high_l].reshape(B, C,
                                                                                                         short * 2,
                                                                                                         short)
        bottom_left = x[:, :, 2 * small_l + 3 * medium_l + high_l:3 * small_l + 3 * medium_l + high_l].reshape(B, C,
                                                                                                               short,
                                                                                                               short)
        bottom = x[:, :, 3 * small_l + 3 * medium_l + high_l:3 * small_l + 4 * medium_l + high_l].reshape(B, C, short,
                                                                                                          short * 2)
        bottom_right = x[:, :, 3 * small_l + 4 * medium_l + high_l:].reshape(B, C, short, short)
    else:
        short = kernel // 2  # 2
        short1 = kernel // 2 + 1  # 3
        l1 = short * short  # 4
        l2 = short * short1  # 6
        l3 = short * kernel  # 10
        l4 = short1 * short1  # 9
        l5 = short1 * kernel  # 15
        l6 = kernel * kernel  # 25


        top_left = x[:, :, :l1].reshape(B, C, short, short)
        top = x[:, :, l1:l1 + l3].reshape(B, C, short, kernel)
        top_right = x[:, :, l1 + l3:l1 + l2 + l3].reshape(B, C, short, short1)
        left = x[:, :, l1 + l2 + l3:l1 + l2 + 2 * l3].reshape(B, C, kernel, short)
        center_block = x[:, :, l1 + l2 + 2 * l3:l1 + l2 + 2 * l3 + l6].reshape(B, C, kernel, kernel)
        right = x[:, :, l1 + l2 + 2 * l3 + l6:l1 + l2 + 2 * l3 + l5 + l6].reshape(B, C, kernel, short1)
        bottom_left = x[:, :, l1 + l2 + 2 * l3 + l5 + l6:l1 + 2 * l2 + 2 * l3 + l5 + l6].reshape(B, C, short1, short)
        bottom = x[:, :, l1 + 2 * l2 + 2 * l3 + l5 + l6:l1 + 2 * l2 + 2 * l3 + 2 * l5 + l6].reshape(B, C, short1,
                                                                                                    kernel)
        bottom_right = x[:, :, l1 + 2 * l2 + 2 * l3 + 2 * l5 + l6:].reshape(B, C, short1, short1)

    top = torch.cat((top_left, top, top_right), dim=3)
    medium = torch.cat((left, center_block, right), dim=3)
    down = torch.cat((bottom_left, bottom, bottom_right), dim=3)
    x = torch.cat((top, medium, down), dim=2)

    return x


def recover_window_ud(x: torch.Tensor, kernel: int) -> torch.Tensor:
    # in:B,C,L , out:B,C,H,W
    B, C, L = x.shape
    short = kernel // 2
    small_l = short * short
    medium_l = short * short * 2
    high_l = short * short * 4

    if kernel / 2 == kernel // 2:
        top_left = x[:, :, :small_l].reshape(B, C, short, short)
        left = x[:, :, small_l:small_l + medium_l].reshape(B, C, short * 2, short)
        bottom_left = x[:, :, small_l + medium_l:2 * small_l + medium_l].reshape(B, C, short, short)
        top = x[:, :, 2 * small_l + medium_l:2 * small_l + 2 * medium_l].reshape(B, C, short, short * 2)
        center_block = x[:, :, 2 * small_l + 2 * medium_l:2 * small_l + 2 * medium_l + high_l].reshape(B, C, short * 2,
                                                                                                       short * 2)
        bottom = x[:, :, 2 * small_l + 2 * medium_l + high_l:2 * small_l + 3 * medium_l + high_l].reshape(B, C, short,
                                                                                                          short * 2)
        top_right = x[:, :, 2 * small_l + 3 * medium_l + high_l:3 * small_l + 3 * medium_l + high_l].reshape(B, C,
                                                                                                             short,
                                                                                                             short)
        right = x[:, :, 3 * small_l + 3 * medium_l + high_l:3 * small_l + 4 * medium_l + high_l].reshape(B, C,
                                                                                                         short * 2,
                                                                                                         short)
        bottom_right = x[:, :, 3 * small_l + 4 * medium_l + high_l:].reshape(B, C, short, short)
    else:
        short = kernel // 2  # 2
        short1 = kernel // 2 + 1  # 3
        l1 = short * short  # 4
        l2 = short * short1  # 6
        l3 = short * kernel  # 10
        l4 = short1 * short1  # 9
        l5 = short1 * kernel  # 15
        l6 = kernel * kernel  # 25

        top_left = x[:, :, :l1].reshape(B, C, short, short)
        left = x[:, :, l1:l1 + l3].reshape(B, C, kernel, short)
        bottom_left = x[:, :, l1 + l3:l1 + l2 + l3].reshape(B, C, short1, short)
        top = x[:, :, l1 + l2 + l3:l1 + l2 + 2 * l3].reshape(B, C, short, kernel)
        center_block = x[:, :, l1 + l2 + 2 * l3:l1 + l2 + 2 * l3 + l6].reshape(B, C, kernel, kernel)
        bottom = x[:, :, l1 + l2 + 2 * l3 + l6:l1 + l2 + 2 * l3 + l5 + l6].reshape(B, C, short1, kernel)
        top_right = x[:, :, l1 + l2 + 2 * l3 + l5 + l6:l1 + 2 * l2 + 2 * l3 + l5 + l6].reshape(B, C, short, short1)
        right = x[:, :, l1 + 2 * l2 + 2 * l3 + l5 + l6:l1 + 2 * l2 + 2 * l3 + 2 * l5 + l6].reshape(B, C, kernel, short1)
        bottom_right = x[:, :, l1 + 2 * l2 + 2 * l3 + 2 * l5 + l6:].reshape(B, C, short1, short1)

    top = torch.cat((top_left, top, top_right), dim=3)
    medium = torch.cat((left, center_block, right), dim=3)
    down = torch.cat((bottom_left, bottom, bottom_right), dim=3)
    x = torch.cat((top, medium, down), dim=2)

    return x


def recover_center_blocks(x: torch.Tensor, kernel: int) -> torch.Tensor:
    # B,4,C,L,,,,B,C,H,W
    B, K, C, L = x.shape  # B,4,C,L

    H = W = int(math.sqrt(L))

    lr_y = x[:, 0, :, :].reshape(B, -1, L)
    ud_y = x[:, 1, :, :].reshape(B, -1, L)
    rl_y = torch.flip(x[:, 2, :, :], dims=[-1]).reshape(B, -1, L)
    du_y = torch.flip(x[:, 3, :, :], dims=[-1]).reshape(B, -1, L)
    y1 = lr_y + rl_y
    y1 = recover_window_lr(y1, kernel)
    y2 = ud_y + du_y
    y2 = recover_window_ud(y2, kernel)
    y = y1 + y2

    return y


class DWMambaLayer(nn.Module):
    """ VSS Layer.
        Args:
            dim (int): Number of input channels.
            attn_drop_rate (float, optional): Attention dropout rate. Default: 0.0
            drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
            norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        """

    def __init__(
            self,
            dim: int = 0,
            drop_path: float = 0,
            norm_layer: Callable[..., torch.nn.Module] = partial(nn.LayerNorm, eps=1e-6),
            attn_drop_rate: float = 0,
            d_state: int = 16,
            **kwargs,
    ):
        super().__init__()
        self.ln_1 = norm_layer(dim)
        self.self_attention1 = W_SS2D(d_model=dim, dropout=attn_drop_rate, d_state=d_state,
                                   **kwargs)
        self.ln_2 = norm_layer(dim)
        self.self_attention2 = C_SS2D(d_model=dim, dropout=attn_drop_rate, d_state=d_state,
                                             **kwargs)
        self.drop_path = DropPath(drop_path)

    def forward(self, input: torch.Tensor):
        x = input + self.drop_path(self.self_attention1(self.ln_1(input)))
        x = x + self.drop_path(self.self_attention2(self.ln_2(x)))
        return x


class DWMambaBlock(nn.Module):
    """ VSS Block.
    Args:
        dim (int): Number of input channels.
        depth (int): Number of blocks.
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
    """

    def __init__(
            self,
            dim,
            depth,
            attn_drop=0.,
            drop_path=0.,
            norm_layer=nn.LayerNorm,
            d_state=16,
            **kwargs,
    ):
        super().__init__()
        self.dim = dim

        self.blocks = nn.ModuleList([
            DWMambaLayer(
                dim=dim,
                drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                norm_layer=norm_layer,
                attn_drop_rate=attn_drop,
                d_state=d_state,
            )
            for i in range(depth)])

        if True:  # is this really applied? Yes, but been overriden later in VSSM!
            def _init_weights(module: nn.Module):
                for name, p in module.named_parameters():
                    if name in ["out_proj.weight"]:
                        p = p.clone().detach_()  # fake init, just to keep the seed ....
                        nn.init.kaiming_uniform_(p, a=math.sqrt(5))

            self.apply(_init_weights)

    def forward(self, x):
        for blk in self.blocks:
            x = blk(x)

        return x


class DWMAKBlock(nn.Module):
    """ DWMAK Block.
        Args:
            dim (int): Number of input channels.
            depth (int): Number of blocks. Default: 0
            drop_rate (float, optional): Dropout rate. Default: None
            attn_drop_rate (float, optional): Attention dropout rate. Default: 0.0
            drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
            norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        """

    def __init__(self, dim, attn_drop_rate=0,
                 drop_path=0, norm_layer=nn.LayerNorm,
                 d_state=16, drop_rate=None, depth=0):
        super().__init__()
        self.ln = norm_layer(dim)
        self.Conv = EFConv(kernel_size=3, stride=1, in_chans=dim, embed_dim=dim)
        self.drop_path = DropPath(drop_path)

        self.VSS = DWMambaBlock(
            dim=dim,
            depth=depth,
            d_state=d_state,
            drop=drop_rate,
            attn_drop=attn_drop_rate,
            drop_path=drop_path,
            norm_layer=norm_layer,
        )
        self.KAN = KANBlock(dim=dim, drop=drop_rate, drop_path=drop_path, norm_layer=norm_layer)

    def forward(self, input: torch.Tensor):
        input = self.VSS(input)

        B, H, W, C = input.shape
        
        x, H, W = self.Conv(self.ln(input).permute(0, 3, 1, 2))
        x = self.KAN(x, H, W).view(B, H, W, C)

        return x


class DWMAKNetEncoder(nn.Module):
    """ Encoder.
            Args:
                patch_size (int): Patch token size. Default: 2.
                in_chans (int): Number of input image channels. Default: 3.
                dim (int): Number of input channels.
                depth (list[int]): Number of VSS blocks. Default: [1, 2, 2, 2]
                feat_size (Union[Tuple[int]]): Number of input channels per stage. Default: [24,48,96,192,384,768]
                drop_rate (float, optional): Dropout rate. Default: 0.3
                attn_drop_rate (float, optional): Attention dropout rate. Default: 0.3
                drop_path_rate (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
                norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
                num_input_channels (int): Number of channels of the input image. Default: 1
                patch_norm (bool): Normalization layer for patchembed. Default: True
            """

    def __init__(self, patch_size=2, depths=[1, 2, 2, 2], feat_size=[24, 48, 96, 192, 384, 768],
                 drop_rate=0.3, drop_path_rate=0.3,
                 norm_layer=nn.LayerNorm, num_input_channels=1, patch_norm=True, attn_drop_rate=0.3, d_state=16):
        super().__init__()
        self.num_layers = len(depths) // 2
        dims = feat_size[2:]
        self.patch_embed = PatchEmbed2D(patch_size=patch_size, in_chans=feat_size[1], embed_dim=feat_size[2],
                                        norm_layer=norm_layer if patch_norm else None)
        self.patch_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        self.layers = nn.ModuleList()
        self.downsamples = nn.ModuleList()

        self.conv = ConvLayer(num_input_channels, feat_size[1])

        for i_layer in range(self.num_layers):
            layer = DWMAKBlock(dim=dims[i_layer],
                              depth=depths[i_layer],
                              drop_path=dpr[i_layer],
                              norm_layer=norm_layer,
                              attn_drop_rate=attn_drop_rate,
                              d_state=math.ceil(dims[0] / 6) if d_state is None else d_state,
                              drop_rate=drop_rate,
                              )

            self.layers.append(layer)
            if i_layer < self.num_layers:
                self.downsamples.append(PatchMerging2D(dim=dims[i_layer], norm_layer=norm_layer))

        for i_layer in range(self.num_layers):
            layer = DWMAKBlock(dim=dims[i_layer+2],
                              depth=depths[i_layer+2],
                              drop_path=dpr[i_layer+2],
                              norm_layer=norm_layer,
                              attn_drop_rate=attn_drop_rate,
                              d_state=math.ceil(dims[0] / 6) if d_state is None else d_state,
                              drop_rate=drop_rate,
                              )

            self.layers.append(layer)
            if i_layer < self.num_layers - 1:
                self.downsamples.append(PatchMerging2D(dim=dims[i_layer+2], norm_layer=norm_layer))

        self.apply(self._init_weights)

    def _init_weights(self, m: nn.Module):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.weight, 1)
            nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    def forward(self, x):
        x_ret = []
        x_ret.append(x)

        x = F.relu(F.max_pool2d(self.conv(x), 2, 2))
        x_ret.append(x)

        x, H, W = self.patch_embed(x)
        x = self.patch_drop(x)

        B, _, C = x.shape
        x = x.view(B, H, W, C)

        for s, layer in enumerate(self.layers):
            x = layer(x)
            x_ret.append(x.permute(0, 3, 1, 2))
            if s < len(self.downsamples):
                x = self.downsamples[s](x)

        return x_ret


class DWMAKNetDecoder(nn.Module):
    """ Decoder.
                Args:
                    num_classes (int): Number of segmentation categories.
                    deep_supervision (bool): deep supervision.
                    depths (list[int]): Number of VSS blocks. Default: [4, 4]
                    feat_size (Union[Tuple[int]]): Number of input channels per stage. Default: None
                    drop_rate (float, optional): Dropout rate. Default: 0.3
                    attn_drop_rate (float, optional): Attention dropout rate. Default: 0.3
                    drop_path_rate (float | tuple[float], optional): Stochastic depth rate. Default: 0.3
                    norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
                """

    def __init__(
            self,
            num_classes: int,
            deep_supervision,
            feat_size: Union[Tuple[int, ...], List[int]] = None,
            drop_path_rate: float = 0.3,
            num_input_channels=None,
            drop_rate=0.3, norm_layer=nn.LayerNorm, attn_drop_rate=0.3, depths=[4, 4]
    ):
        super().__init__()

        seg_layers = []
        self.deep_supervision = deep_supervision
        channels = feat_size[::-1]  # 768,384,192,96,48,24

        self.decoder1 = D_ConvLayer(channels[5])
        self.decoder2 = D_ConvLayer(channels[4])
        self.decoder3 = D_ConvLayer(channels[3])
        self.decoder4 = D_ConvLayer(channels[2])
        self.decoder5 = D_ConvLayer(channels[1])


        self.conv5 = nn.Conv2d(in_channels=channels[0] + channels[1], out_channels=channels[1], kernel_size=1)
        self.norm5 = nn.BatchNorm2d(channels[1])
        self.conv4 = nn.Conv2d(in_channels=channels[1] + channels[2], out_channels=channels[2], kernel_size=1)
        self.norm4 = nn.BatchNorm2d(channels[2])
        self.conv3 = nn.Conv2d(in_channels=channels[2] + channels[3], out_channels=channels[3], kernel_size=1)
        self.norm3 = nn.BatchNorm2d(channels[3])
        self.conv2 = nn.Conv2d(in_channels=channels[3] + channels[4], out_channels=channels[4], kernel_size=1)
        self.norm2 = nn.BatchNorm2d(channels[4])
        self.conv1 = nn.Conv2d(in_channels=channels[4] + num_input_channels, out_channels=channels[5], kernel_size=1)
        self.norm1 = nn.BatchNorm2d(channels[5])
        self.dropout5 = nn.Dropout(0.2)
        self.dropout4 = nn.Dropout(0.15)
        self.dropout3 = nn.Dropout(0.1)
        self.dropout2 = nn.Dropout(0.05)


        for s in range(3):  # for deep supervision
            input_features_skip = channels[s + 2]
            seg_layers.append(nn.Conv2d(input_features_skip, num_classes, 1, 1, 0, bias=True))
        # for final prediction
        seg_layers.append(nn.Conv2d(channels[-1], num_classes, 1, 1, 0, bias=True))
        self.seg_layers = nn.ModuleList(seg_layers)

        self.apply(self._init_weights)

    def _init_weights(self, m: nn.Module):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.weight, 1)
            nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, skips):

        seg_outputs = []
        outs = []

        x = skips[-1]
        x = F.relu(F.interpolate(x, scale_factor=(2, 2), mode='bilinear'))
        x = self.norm5(self.conv5(torch.cat((x, skips[-2]), dim=1)))
        x = self.decoder5(x)
        x = self.dropout5(x)


        x = F.relu(F.interpolate(x, scale_factor=(2, 2), mode='bilinear'))
        x = self.norm4(self.conv4(torch.cat((x, skips[-3]), dim=1)))
        x = self.decoder4(x)
        outs.append(x)
        x = self.dropout4(x)

        x = F.relu(F.interpolate(x, scale_factor=(2, 2), mode='bilinear'))
        x = self.norm3(self.conv3(torch.cat((x, skips[-4]), dim=1)))
        x = self.decoder3(x)
        outs.append(x)
        x = self.dropout3(x)

        x = F.relu(F.interpolate(x, scale_factor=(2, 2), mode='bilinear'))
        x = self.norm2(self.conv2(torch.cat((x, skips[-5]), dim=1)))
        x = self.decoder2(x)
        outs.append(x)
        x = self.dropout2(x)

        x = F.relu(F.interpolate(x, scale_factor=(2, 2), mode='bilinear'))
        x = self.norm1(self.conv1(torch.cat((x, skips[-6]), dim=1)))
        x = self.decoder1(x)

        for s in range(3):
            if self.deep_supervision:
                seg_outputs.append(self.seg_layers[s](outs[s]))

        seg_outputs.append(self.seg_layers[-1](x))

        seg_outputs = seg_outputs[::-1]

        if not self.deep_supervision:
            r = seg_outputs[0]
        else:
            r = seg_outputs

        return r


class DWMAKNet(nn.Module):
    def __init__(self, encoder_args, decoder_args):
        super().__init__()
        self.encoder = DWMAKNetEncoder(**encoder_args)
        self.decoder = DWMAKNetDecoder(**decoder_args)

    def forward(self, x):
        skips = self.encoder(x)
        out = self.decoder(skips)

        return out

            
def get_dwmaknet_from_plans(
        plans_manager: PlansManager,
        dataset_json: dict,
        configuration_manager: ConfigurationManager,
        num_input_channels: int,
        deep_supervision: bool = True,
):
    dim = len(configuration_manager.conv_kernel_sizes[0])
    assert dim == 2, "Only 2D supported at the moment"
    label_manager = plans_manager.get_label_manager(dataset_json)

    encoder_args = dict(
        feat_size=[24, 48, 96, 192, 384, 768],
        drop_path_rate=0.3,
        drop_rate=0.3,
        attn_drop_rate=0.3,
        depths=[1, 2, 2, 2],
        num_input_channels=num_input_channels
    )
    decoder_args = dict(
        num_classes=label_manager.num_segmentation_heads,
        deep_supervision=deep_supervision,
        feat_size=[24, 48, 96, 192, 384, 768],
        drop_path_rate=0.3,
        drop_rate=0.3,
        attn_drop_rate=0.3,
        num_input_channels=num_input_channels
    )

    model = DWMAKNet(encoder_args, decoder_args)
    model.apply(InitWeights_He(1e-2))
    model.apply(init_last_bn_before_add_to_0)

    return model

