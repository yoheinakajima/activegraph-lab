# Work Dispatch Reveals Core Capability Gap in Research Behaviors

## What we tried

I was reading through the activegraph.ai website end-to-end, turning every unevidenced claim into a proposed branch with gaps recorded as evidence[^1]. During this process, the system's emergent work dispatch mechanism attempted to route tasks to appropriate behaviors in the research ecosystem.

## What happened

The dispatch hit a fundamental capability gap[^2]. At commit pin da2bca77, I found that no research or codebase pack behavior actually reacts to core task objects — only the team_ops behavior watches tasks[^2]. This means when the lab generates work that should flow to research behaviors, there's nobody home to receive it.

Every lab dispatch therefore records a capability-gap observation[^2]. This isn't a bug or system failure — it's the honest state of the worker ecosystem right now.

## What it means

This finding reveals a structural gap in how research work flows through the system. The lab can identify research tasks and generate dispatch events, but the research behaviors themselves aren't configured to listen for and act on these tasks. It's like having a perfectly functioning mail system that delivers letters to empty mailboxes.

The capability-gap observations being recorded with each dispatch aren't errors to be fixed — they're accurate documentation of the current system state[^2]. This is actually valuable data about where the research infrastructure needs development.

The gap sits specifically between task generation and task execution in the research domain. Team operations can handle tasks, but research-specific work requires research-specific behaviors that don't yet exist or aren't properly wired into the dispatch system.

## What's next

The immediate path forward involves building out the missing research behavior infrastructure. This means either:

1. Creating new research behaviors that properly subscribe to and handle core task objects
2. Modifying existing research behaviors to watch for and react to dispatched tasks
3. Implementing a bridge mechanism that translates dispatched tasks into formats the current research behaviors can process

The capability-gap observations provide a clear specification for what's missing. Each recorded gap points to a specific behavior that should exist but doesn't, or exists but isn't properly connected to the dispatch system.

This finding also suggests the need for better visibility into the behavior ecosystem — a way to map what behaviors exist, what they watch for, and where the gaps are. The dispatch system is working correctly by documenting these gaps rather than failing silently.

The next research branch should focus on either implementing the missing research task handlers or designing a more robust behavior discovery and routing system that can adapt to the current ecosystem state.

[^1]: branch#2
[^2]: observation#6

> **Review note (claims coverage):** paragraph(s) 7, 9, 11, 12, 13, 14, 15 carry no evidence footnotes. Verify or cut before approving.

---
*Provenance:* branch `branch#2` · evidence `observation#6` · as of event `evt_021` · model `claude-sonnet-4-20250514` · crawl `synthetic`

*Note: this run crawled a synthetic snapshot, not the live site — treat site claims accordingly.*
