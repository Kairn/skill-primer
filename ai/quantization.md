## Tradeoff

Quantization refers to representing parameters in a lower-precision format (e.g. from FP16 to FP8). Doing so can reduce the effective model size, memory bandwidth usage, and floating point operations at the cost of lower quality output. Quantization is much more common in inference, though quantization-aware training exists. Quantization is not just a single unified knob; it can be applied at different layers of the transformation process with different memory/compute/quality impact and may require hardware support for certain cases. Choosing the right quantization configuration is about carefully weighing the tradeoffs given the model and usage patterns.

## Methodology

Quantization/dequantization is basically number encoding/decoding (lossy). With static (offline) quantization, weights are scaled down and frozen offline, and they are restored to original-precision in process when needed by the inference engine. Dequantization needs 3 things per given tensor:
1. **The low-precision code** - value in fewer bits, replacing original value.
2. **The scale** - constant used to upscale the code.
3. **The decode rule** - what derives the resulting value using the 2 values above. It can be a fixed formula or a quick lookup step.

Static scales are computed by running calibration with the given model, a process of measuring how the model behaves on representative data, so that we can pick scales (or make rounding decisions) that fit real values produced during intermediate operations. Each scale value has a scope (a group of weights) it applies to, and having granular scales improves accuracy with more overhead.

Scales can also be dynamically calculated during inference, applicable to dynamic values such as activations and KV cache.

### Number Format
For weights, integer formats (e.g. INT8 and INT4) are common, and floating point formats (e.g. FP8 and FP4) are common in activations and weights. The primary difference between INT and FP formats is the uniformity; INTs are uniformly spaced whereas FPs have dynamic range. Because of this, FP8 tolerates outliers better than INT8 and often requires less calibration machinery, but integers are cheaper and more power-efficient to work with.

To further reduce errors caused by outliers (often due to activations instead of weights), especially with 4-bit values, strategies such as AWQ identifies weight channels that have large activation magnitude on calibration data and scales up the weights (and scales down activations) there to achieve less relative error. The effect is similar to keeping "important weights" (~1%) in 16-bit only without actually doing so because that would be awkward/slow for hardware to handle when precisions are mixed.

## Application

Knowing the [transformation math](../ai/transformer.md) is essential to understand where the savings come from.

### Weights
Weight-only quantization means keeping the model parameters only in lower precisions, and the impact to quality is typically the smallest at this level. Quantizing the weights reduces the model size directly (2x in FP8 and 4x in FP4) and thus saving VRAM space and the memory bandwidth needed during decoding. It does not require special hardware support; only stored values change and they are upscaled before calculations. **This is a pure memory lever with NO saving in compute**, and in fact, it introduces additional overhead in compute (prefill typically is slower) due to added scaling math.

This strategy is the most effective at decoding with a small batch size where a large portion of the bandwidth is used for model weights. It is also a directly enabler for running models that don't fit the VRAM in FP16.

### Activations
Activation quantization means running **activations flowing into a matrix multiplication** in lower precision which includes essentially **every linear projection** (not including `Q × Kᵀ` and `S × V`) in the transformer where both operands are quantized. Because calculations are scaled down, weights are also quantized to the same or lower precision when this is enabled. Activation quantization effectively scales up the compute capacity of the GPU and will require native lower precision tensor cores to be enabled; W8A8 (8-bit for weights and activations) achieves 2x the peak throughput for matmuls and requires Nvidia Hopper (or equivalent/newer) GPUs, and W4A4 achieves 4x the throughput and requires Blackwell or newer.

**This is a direct compute lever** (without considering the weights quantization that it comes with) and is the most effective at speeding up prefill which is typically compute-bound. Realistic gain is usually 1.3x-1.7x due to extra overhead and computations that remain in full precision.

### KV
As the name implies, KV quantization means storing the KV cache in a lower-precision format which saves VRAM and bandwidth usage the same way weight-only quantization does. It is the most effective at higher batch size (or supporting longer sequences) where KV cache is significant. This typically has a higher quality impact than weights quantization, but a standard FP8 quantization (storage only) with 16-bit attention math is nearly lossless and widely deployed. FP4 on the other hand is more risky and requires careful tuning.

Attention quantization (`Q × Kᵀ`) is sometimes used to improve prefill performance further but the quality loss can be severe due to compounding errors and the output having huge dynamic range. Storage-only quantization will not need special hardware whereas attention quantization requires tensor core support at lower precision.
