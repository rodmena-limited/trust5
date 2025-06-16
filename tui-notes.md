we need another set of features for the TUI.

we want to cut the TUI into two columns, the left column, will be the Trust5's current coding mode,
where the proper full software is being implemented, no change on that.
the top header and status bar should remain the same
the middle big area should be two columns, 60% left (current stuff) and 40% right column.
The right column should be two rows. top row should be summary of what's going on the left side.
so another independent llm call will happen, will send last 32K of content from the left to a fast LLM and will show
markdown proper rendered summary, something like (example)

===========
- The verification is ongoing and Trust5 is checking if all tests are proper
- The codebase has written 5 modules including ... files and 10 tests with 200 functions
- in the next steps we'll check if security is correct or not .....
==========

Essnetially will summarize everything is happening and will happen. this should look into all context, workflow, todo, etc.
this should call LLM with one correct context and should expect very especific output that can  be beautifully put on the top row of the right column.

the bottom row will a input box from user, where user can ask questions about the UI or essentially any question.
The buttom row is the next phase, jsut create the UI for now and we'll implement the funcitonality later.


Again, like the all context and information, it should be operated by clear 4 character codes, should be very well engineers, via event-bus, non-blocking, very low latency, etc.
