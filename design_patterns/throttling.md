## Practical Throttling Algorithms

Implementing the right throttling mechanism for a system needs careful consideration of its enforcement rule(s), call volume, ecosystem, error tolerance, and degree of sharding/distribution. An expensive "sliding window log" implementation is not strictly wrong if the rule enforces exactly N or fewer calls within a time window (common in security-sensitive functions such as checking login attempts), and this is typically protected by an outer limiter running a coarse-grained algorithm. Most real-world throttling algorithms give up a little bit of accuracy guarantee for efficiency at high request rates.

### Sliding Window Counter
A **statistically accurate** counter for a sliding window popularized by Cloudflare. It is extremely memory efficient (2 counters per tenant); define a window (e.g. 1 minute) and a limit (e.g. 100), and it tracks the number of calls in the previous (fixed, e.g. 10:00 - 10:01) window and the calls in the current window (e.g. 10:01 - now) and calculate the sliding window count **assuming calls in the previous window are uniform**. For example, at time 10:01:30, we are 50% through the current fixed window, so the estimated count for window 10:00:30 - 10:01:30 is `0.5 * previous_window_count + current_window_count`.

This algorithm does NOT have hard accuracy guarantee and lacks a way to express rate and burst separately, but **the additive nature of the counters and the triviality of the mutation (i.e. increment) make it a great choice for a highly distributed rate limiter**, where guaranteeing atomicity and merging counters from multiple sources are easy. This is why Cloudflare uses it for high-cardinality abuse protection at the edge.

### Token Bucket
This is the workhorse for most public APIs. Each tenant is allocated a bucket of certain depth (burst) with a refill rate (request rate), and each allowed request deducts tokens from the bucket. Refill is calculated lazily upon access, so no timer is needed (only store last access timestamp) which makes it efficient and simple to implement.

Unlike sliding window counter, token bucket has a hard bound for error (much smaller in adversarial case), and its mental model is very intuitive to grasp.

### GCRA
GCRA (Generic Cell Rate Algorithm) is a less known algorithm but mathematically equivalent to token bucket. **The brilliant part is that it requires only one timestamp to track state, and this is called theoretical arrival time (TAT)**. Intuitively, TAT represents the expected arrival of the next call if the calls are coming at the allowed rate, and it can either be now or somewhere in the future but never in the past. For example, if rate is 1 per second, we expect calls to arrive at second 0, 1, 2, and so on. TAT initializes to the current timestamp (allow any call), and each time a call arrives, TAT is pushed back by `1 / rate` (1 second here), and this is how burst is expressed: calls may be allowed when TAT is in the future, but that must be compensated by a delayed call later if client wants to get that credit back. Calls are rejected when TAT is larger than the burst tolerance.

In practice, GCRA is slightly more efficient than token bucket and atomicity is achieved more easily since only 1 value is updated.

## Other Tricks

### Scaling
When the application or system is large enough that requests must be distributed to multiple machines, the same goes for the throttler. Because every throttling algorithm involves mutable states, the most ubiquitous solution is to add a central store (e.g. Redis) that all servers talk to. The central store is also commonly sharded to partition the traffic (e.g. via client ID).

Quota splitting (divide one's global rate into smaller buckets) is also a common strategy in those systems in order to handle hot keys and/or traffic served by isolated clusters (e.g. America vs. EU). The algorithm doesn't change but applied to sub-buckets independently.

### LLM Inference
Throttling in LLM inference APIs differs from classic APIs because the cost (e.g. output tokens) per request is unknown/dynamic and we must also consider customers' spend budget and our capacity. Common strategies include:
* **Reserve on admission then adjust** - deduct max_tokens or an estimate of the output tokens count once the request is allowed, and after the generation is complete, refund or charge the difference.
* **Separation of quota and capacity** - let the throttling system focus on quota (fairness and budget) only using classic algorithms (e.g. token bucket) and let the inference engine handle scheduling, batching, and load-shedding decisions when GPUs are under load.
* **Asynchronous/Offline reconciliation** - the throttling system handles live traffic control and is allowed to have small errors, whereas an offline reconciliation system collects actual usage emitted from gateways for billing and accounting. This information can also be fed into the throttler for usage limits checks over a longer period of time (per month).
