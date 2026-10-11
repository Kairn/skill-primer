## SMP and AMP

With modern CPUs, operating system needs to distribute and manage tasks across multiple processor cores. This is much more complex than a single worker taking tasks from a single conveyor. **Asymmetric Multiprocessing (AMP)** uses a master processor as the dedicated scheduler to allocate work to other (slave) cores, whereas **Symmetric Multiprocessing (SMP)** lets each processor independently manage its private run queue and make scheduling decisions.

AMP is an easier architecture because it is essentially a "single-threaded" scheduler, and this idea can work in smaller scale services, but it creates a permanent bottleneck at the "master". At OS level, AMP generally will not suffice due to the granularity of tasks and the typical modern system load. **All modern OSes (Linux, macOS and Windows) use SMP exclusively**.

## The Linux Scheduler

Nowadays, the default Linux scheduler is called **Earliest Eligible Virtual Deadline First (EEVDF) scheduler** (replaced Completely Fair Scheduler). This is the scheduler for running pretty much every user application. It looks at 2 main criteria when deciding the next task to run:
* Eligibility (Fairness) - kernel keeps track of CPU time a task should get (its fair share in the queue, determined primarily by nice level) vs. what it actually got and those that are owed time become eligible for scheduling.
* Virtual deadline (Latency) - kernel "estimates" a deadline for each eligible task and prioritize the ones due earlier. Deadline is computed from nice level as well as a task's dynamic behavior (e.g. an interactive task that releases CPU quickly gets a shorter deadline).

### Scheduling Classes
For strictly higher priority work such as CPU migration (internal) and real-ime tasks (e.g. system audio), Linux implements different scheduling classes and they are checked in order. EEVDF only governs the "normal" scheduling class which is below all other stricter classes, and that's how Linux guarantee CPU time for tasks with non-negotiable deadlines (common in specialized devices).

## Coordination

In SMP architecture, each CPU owns a private run queue and runs its own instance of the scheduling algorithm, but they are also aware of their neighbors and actively coordinate with each other to achieve load balance and efficiency.

### Task Distribution
By default, when a new program starts, the kernel tries to place it in the same queue as its fork parent process, if the CPU is "relatively" idle. Otherwise, the scheduler scans the system for idle cores.

Also, if the system is already unbalanced (e.g. from a herd of threads started in rapid succession), an idle core actively tries to "steal" work from busy cores to restore balance.

### Hardware Affinities
When tasks need to be allocated or migrated to different cores, the system follows a few affinity rules to pick the best one:
* Cache - sibling threads on the same CPU socket share the same L1/L2 cache. Kernel resists moving tasks to other cores without warm cache data for the task.
* NUMA - in Non-Uniform Memory Access (NUMA) architectures, memory access is much faster in the same region (local). Hence CPUs in the same NUMA region are preferred.
* Energy profile - modern chips have power-saving cores and high-performance cores, and kernel can use the task's historical footprint to determine the type of core better suited for it.
