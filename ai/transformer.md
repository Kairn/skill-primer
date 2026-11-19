## Auto-regressive Generation

The vast majority of frontier models are auto-regressive (sequential, left-to-right) at their core. The transform process is thus essential to inference engineering and optimization efforts. By understanding the computation steps in token generation, we gain insights on the main bottlenecks that limit performance and the strategies that tackle them.

## Attention and KV Cache

The major breakthrough in modern LLM is the attention mechanism where each subsequent token "attends" to (all) previous tokens (the context), establishing the causal relationship between tokens from left to right. Attention information is cached (KV cache) in GPU VRAM to avoid repeated computation when generating each new token. Depending on the model size, sequence length, concurrency, KV cache can take a significant portion of the GPU memory.

Each "KV" is a pair of vectors the size of the model's head dimension. To calculate the KV size per token, we multiply the size of each KV pair by the number of the attention heads and by the number of hidden layers. For example, for Llama-3-70B, each token costs 2 (bytes) * 2 (K + V) * 128 (head dimension) * 8 (GQA heads) * 80 (layers) = 320 KB in GPU memory in FP16/BF16. A single sequence of 1000 tokens takes up 320 MB which can quickly add up with concurrency.

## Transformation

To generate the next token, the previous input goes through the a series of transformation stages and eventually becomes a probability distribution of all tokens in the vocabulary, and the sampling logic picks one. The previous input is the previous token during each decode step, or it is the entire context during prefill (first token generation). Although the math is identical in prefill and decode, prefill has a much higher arithmetic intensity because the KV cache must be calculated for the full input in request before the first token can be derived.

In the following walkthrough of single token generation, we will use the Llama-3-70B model with GQA and SwiGLU FFN with these parameters:
* Model dimension (d_m) = 4096
* Head dimension (d_h) = 128
* Query heads = 64 (8 for GQA)
* Vocabulary size = 128256
* FFN intermediate size (d_ff) = 14336

### Embedding

### Attention

### FFN

### Output
