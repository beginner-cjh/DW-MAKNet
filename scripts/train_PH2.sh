# V1 can load pretrained model of VSS Block

MAMBA_MODEL=$1
PRED_OUTPUT_PATH="data/nnUNet_results/Dataset123_PH2/${MAMBA_MODEL}__nnUNetPlans__2d/pred_results"
TRUE_IMAGE_PATH="data/nnUNet_raw/Dataset123_PH2/imagesTs"
TRUE_LABEL_PATH="data/nnUNet_raw/Dataset123_PH2/labelsTs"
GPU_ID="0"

# train
CUDA_VISIBLE_DEVICES=${GPU_ID} nnUNetv2_train 123 2d all -tr ${MAMBA_MODEL} -num_gpus 1 #&&
# predict
echo "Predicting..." &&
CUDA_VISIBLE_DEVICES=${GPU_ID} nnUNetv2_predict \
    -i "${TRUE_IMAGE_PATH}" \
    -o "${PRED_OUTPUT_PATH}" \
    -d 123 \
    -c 2d \
    -tr "${MAMBA_MODEL}" \
    --disable_tta \
    -f all \
   -chk "checkpoint_best.pth" &&

echo "Computing iou and F1..."
python evaluation/PH2_metrics.py \
    -t "${TRUE_LABEL_PATH}" \
    -p "${PRED_OUTPUT_PATH}"&&
    
echo "Done."
