## Auto-regressive Generation

The vast majority of frontier models are auto-regressive (sequential, left-to-right) at their core. The transform process is thus essential to inference engineering and optimization efforts. By understanding the computation steps in token generation, we gain insights on the main bottlenecks that limit performance and the strategies that tackle them.

## Attention and KV Cache

The major breakthrough in modern LLM is the attention mechanism where each subsequent token "attends" to (all) previous tokens (the context), establishing the causal relationship between tokens from left to right. Attention information is cached (KV cache) in GPU VRAM to avoid repeated computation when generating each new token. Depending on the model size, sequence length, concurrency, KV cache can take a significant portion of the GPU memory.

Each "KV" is a pair of vectors the size of the model's head dimension. To calculate the KV size per token, we multiply the size of each KV pair by the number of the attention heads and by the number of hidden layers. For example, for Llama-3-70B, each token costs 2 (bytes) * 2 (K + V) * 128 (head dimension) * 8 (GQA heads) * 80 (layers) = 320 KB in GPU memory in FP16/BF16. A single sequence of 1000 tokens takes up 320 MB which can quickly add up with concurrency.

## Transformation

To generate the next token, the previous input goes through the a series of transformation stages and eventually becomes a probability distribution of all tokens in the vocabulary, and the sampling logic picks one. The previous input is the previous token during each decode step, or it is the entire context during prefill (first token generation). Although the math is identical in prefill and decode, prefill has a much higher arithmetic intensity because the KV cache must be calculated for the full input in request before the first token can be derived.

In the following walkthrough of single token generation, we will use the Llama-3-70B model with GQA and SwiGLU FFN with these parameters:
* Model dimension (`d_m`) = 8192
* Head dimension (`d_h`) = 128
* Query heads = 64 (8 for GQA)
* Vocabulary size = 128256
* FFN intermediate size (`d_ff`) = 28672

### Embedding
Every token in the input is first mapped to a vector of size `d_m` by looking up a fixed dictionary. This is called embedding which takes an arbitrary token in human language and turns it into a series of floating point numbers that we can run computations on. Embedding is typically done outside the GPU as it only requires static lookup.

Using the example model, after embedding, the resulting vector has the shape [1, 8192] during decoding, and in prefill, the input will be a matrix of shape [T, 8192] where T is the context length in tokens which is all the individual vectors stacked. This vector is also called "residual stream" because all the following steps keep transforming it and accumulating residuals onto it before projecting into the output.

### Attention
Token vector (or matrix) is projected into Q, K, V vectors using fixed projection weights in the model, in our example model:
* Q = [T, 8192] (residual) × [8192, 8192] (Q weight block) = [T, 8192]. Given the model has 64 attention heads, it is also reshaped/viewed as [T, 64, 128].
* K = [T, 8192] (residual) × [8192, 1028] (K weight block) = [T, 1028]. This is 1/8 size of Q because each group is shared by 8 attention heads. Reshaped as [T, 8, 128].
* V = [T, 8192] (residual) × [8192, 1028] (V weight block) = [T, 1028]. Same as K in shape.

Each KV pair serves a group of attention heads (GQA). This reduces the size of KV cache; Q is never cached. Before KV is cached, rotary position embedding is applied to the vectors to encode the relative position of tokens (differentiate [a, b, c] from [c, a, b]).

Attention is then calculated in the following steps:
1. S = Q × Kᵀ (per head) = [T, 128] (per head Q) × [128, C] (K for all context before). The final shape is [64, T, C] for all heads.
2. M = S × V (per head) = [T, C] (S per head) × [C, 128] (V for all context before). Then we merge all heads and the final shape comes up to [T, 128 * 64] = [T, 8192].
3. O = [T, 8192] × [8192, 8192] (attention output weight block) = [T, 8192].

The final O is added to the residual before processing FFN.

### FFN
While attention is meant for capturing the language context, FFN (Feed-Forward Network) can be thought of as the "knowledge base" of the model that transforms input data into "answers". From the attention output, it does the following:
1. Gate projection - G = [T, 8192] (attention O) × [8192, `d_ff`] (gate weight block) = [T, 28672].
2. Up projection - U = [T, 8192] (attention O) × [8192, `d_ff`] (up weight block) = [T, 28672]. Same shape as G.
3. Activation - use an activation function (1-to-1 deterministic number mapping, e.g. SwiGLU) to map G into G'. Each element in G is transformed using the function.
4. Gating - multiply U with G' element-wise. No matrix multiplication. Result is still in shape of [T, 28672].
5. Down projection D = [T, 28672] × [28672, 8192] (down weight block) = [T, 8192].

The resulting D is in the same shape as input and is added to the residual before processing in the next layer.

### Mixture of Experts (MoE)
Many large frontier models are MoE models which divides the full FFN into many partitions (experts). Instead of operating with the full dense FFN (above example), a routing layer selects a small set of partitions based on the context and join these partitions together to be used as the FFN blocks. This strategy drastically decreases the amount of computation involved in FFN as a large portion of the model parameters are not used (i.e. a much smaller `d_ff` effectively). **Attention computation is NOT changed with MoE**.

### Output
Attention and FFN operations are applied for each layer of the LLM. Llama-3-70B has 80 intermediate layers with different projection weights (QKV, GUD) on each. Once all layers finished processing, the last token's residual stream ([1, 8192]) is projected to vocabulary size: [1, 8192] × [8192, 128256], and the result is a single vector of size of the vocabulary. After scaling and applying softmax, it becomes the probabilities of each token in the vocabulary in the next position. The final step is to sample a single token from it (e.g. the one with highest probability). The sampled token becomes the output token and also the input for computing the next, and the loop continues until a stop token is generated or the limit is reached.
