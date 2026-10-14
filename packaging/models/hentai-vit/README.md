# Sentinel Hentai Classifier Model

Sentinel runs the `carbon225/vit-base-patch16-224-hentai` ViT classifier through ONNX Runtime.

The production model file must be installed as:

```text
/usr/share/sentinel/models/hentai-vit/model.onnx
```

The source-tree packaging location is:

```text
packaging/models/hentai-vit/model.onnx
```

The ONNX export uses the original three-class label order:

1. `safe`
2. `suggestive`
3. `explicit`

Runtime preprocessing is implemented directly in `hentai_api.py` and reproduces the original ViT image processor:

- RGB input
- 224 x 224 resize
- bilinear interpolation
- rescale by `1 / 255`
- mean `[0.5, 0.5, 0.5]`
- standard deviation `[0.5, 0.5, 0.5]`

The original Sentinel blocking gates are preserved unchanged. The ONNX file is intentionally not included in ordinary source ZIP exports because of its size and should be distributed through the project's model artifact/LFS packaging process.
