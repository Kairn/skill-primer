## Motivation

Auto-regressive generation emits one token at a time and each step is sequential which creates a bottleneck for memory bandwidth at low batch sizes when most of the compute capacity is not utilized during decoding. However, if we can somehow "foresee" multiple tokens that come after, it is possible to verify them together in a single step similar to prefill and amortize the memory movement cost of the model weights. Speculative decoding is based on this very concept, and it involves 3 common steps at a high level:
1. During each decode step, a drafter (could be deterministic logic or a small LLM) will draft a few tokens after the current position.
2. All draft tokens are packed into the input tensor for the transformer, and this will yield the outcome for each drafted token for the target model.
3. Going from left to right, if the sampler would have picked the same token as the draft, the token is accepted; otherwise, that token is rejected as well as everything that follows. Then, a bonus token is generated at the end (1 token guaranteed still even if the entire draft is wrong).

The verification logic ensures that the speculation is completely lossless. If the drafter is well designed, this can save significant bandwidth for each token and improve throughput when spare compute is available (e.g. small batches) for the verification overhead. Even if compute is relative saturated at high concurrency, speculative decoding is still a lever to reduce decode latency (i.e. ITL) in exchange for lower overall throughput.

## Methods

The effectiveness of speculative decoding essentially hinges on two things: the cost of drafting and the acceptance rate of the drafts. This leads to very different approaches depending on the use-case/domain. One drafter that excels in guessing tokens for one type of target model might fail completely for another. Specialized training of the drafter model and extensive tuning are typically required for production success.

### LLM Drafter
Earlier approaches of LLM drafting involves deploying a smaller scaled model in the same model family (e.g. 1B for 70B) that drafts candidate tokens for the target model. This is easy to deploy, widely available with no special training, and moderately effective when models are closely aligned. Nowadays, the modern approaches such as EAGLE reuses the hidden states of the target model to achieve a high acceptance rate with only a single transform layer; this is cheaper to run, but does require training the EAGLE drafter. EAGLE is currently considered the best option for general purpose chat and reasoning.

In EAGLE-3, at each step, the previous hidden state output (residual stream) at different layers (typically 3, one early, one middle, and one late) are stacked and fused into one stream of the same length. This stream is concatenated with the new embedding (total size `2 * d_m`) and fed into the EAGLE transformer (same attention/MLP math) to predict the next draft token, and the hidden state output is feed back into itself until enough tokens are drafted. Then, the target model will verify them in one step and keep the accepted tokens.

A more advanced drafting logic of EAGLE builds a token tree of various drafting paths from the latest position and verify all the paths (packed into the same tensor) in the same decoding step for an even higher average throughput. The intuition is that, if there are 2 competing candidate (each with ~50% chance being correct) for a draft position, instead of forcing a guess and pay a high price for being wrong, explore both paths and build out a few branches that can all be potentially promising, and verify all paths to pick the longest one.

### Retrieval Drafter
Another popular approach is to simply draft tokens by matching the last a few tokens with an existing dictionary and propose whatever followed there (suffix); the dictionary can be the prompt itself, a fixed datastore, or something built dynamically from earlier inputs. These drafters don't require additional model or training and cost very little to run, and they can be very effective (easily beats EAGLE) for specific use cases such as summarization, RAG, document Q/A, and code editing.
