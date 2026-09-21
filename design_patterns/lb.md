## Software Load Balancer Domination

Load balancing nowadays is largely a software (implementation) driven problem. Commodity hardware is fast enough and very cost effective to be used as LBs, with several advantages:
* **Programmability matters.** Custom routing logic, integration, and discovery is only possible when software is programmable.
* **Elasticity.** Capacity scales with demand. Dynamic sizing without having to commit to a fixed expense is valuable.
* **Cloud-native built-in solutions.** AWS/GCP/Azure and other cloud platforms provide them by default. Easy integration and migration.

:warning: Although it is NOT strictly wrong to throw in hardware LBs, they are relegated to legacy and super specialized use cases. More importantly, little depth comes out of discussing them because you have no control over the box.

## L4LB

While Layer 7 load balancer (L7LB, e.g. AWS ALB) is the most ubiquitous actor in a client-server application due to its ability to route by inspecting the HTTP request directly, Layer 4 load balancer (L4LB, e.g. AWS NLB) is a powerful tool often used with L7LBs.

L4LB has these traits worth remembering when designing a large system:
* Very fast and uses less CPU but can only route by IP/port. No content based routing.
* Cannot terminate connection or act as a full proxy like a L7LB. It is a packet forwarding box.
* Best place to emit connection level stats for observability.
* Absorbs the raw scale and DDoS surface. It can make quick decisions or drop packets without spending CPU on the expensive L7 path.

### Placement and Importance
L4LB sits between DNS and L7LB fleet. DNS can do coarse routing (e.g. picking region/cluster) but its protocol is limited and DNS TTL is typically too long and unreliable to achieve the needs of a large service.

But going directly to L7 has severe downsides other than pure scaling concerns, such as no failure isolation or smart L7 fleet health check. Adding L4LB removes bad L7 nodes automatically and achieves zero-downtime L7 deployment, which DNS failover alone is too slow for.

:bulb: AWS/GCP bundles an L4 tier with their managed HTTP LB offerings for free, highlighting its importance.

### eBPF Optimization
Any **software L4LB running on Linux** can leverage eBPF/XDP to further optimize packet processing compared to default software (e.g. iptables). eBPF programs run directly inside the Linux Kernel and can intercept packets without entering user space, before reaching the networking stack, and it can achieve very high throughput on commodity hardware. Note, L7 protocol cannot be understood at this layer due to how early interception happens in the data path.

## Client Side Load Balancing
Often an overlooked aspect in system design. Client side LB is implemented without a centralized server/fleet, instead, the clients themselves decide how to send traffic. It is applicable only for east-west (internal) traffic which is why public services never touch it.

A common approach in modern systems is to deploy a sidecar (e.g. Envoy) alongside each node that intercepts traffic and acts as a proxy between internal services. Sidecar is kept up-to-date with a control plane service, and it can also handle retry, encryption, metrics emission, and other utilities for free. Typically used in large enterprises as a common client implementation for its microservice network.

## Practical Balancing Algorithms

Simple load balancing strategies (e.g. round robin, least connections, or weighted variants) almost always fall short in real scenarios with a sizable routable targets. Consider these when tackling a real design task:
* **P2C (Power of Two Choices)** - picks 2 (or a few) backends at random, and send to the one with least load/connections. Simple yet powerful and very scalable. Default to this for most cases.
* **Consistent hashing w. bounded loads** - hash over IP/session, sticky routing. Very useful for session/cache locality sensitive services (e.g. LLM inference). It can create hot spots when left on default, hence a load cap can stop any single instance from getting overwhelmed.
* **Latency-aware routing w. capacity planning** - dynamic routing based on EWMA latency, adaptive and favor user experience. Also needs capacity/load management to avoid hot spots or network congestion. Complex and reserved for sophisticated applications (e.g. CDN providers).
