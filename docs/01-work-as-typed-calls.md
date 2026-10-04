# Work as a program of typed calls

*Status: an idea. It frames the formats in this repository. It is not a specification, and no
runtime that implements it is part of this repository.*

## The frame

Treat a body of work as a program. The program is a set of calls. Each call takes a typed input and
returns a typed output. A call can also have an effect, for example it writes a file, or it can
change stored state. So the program is not purely functional.

Every call has an implementation. There are two kinds:

- A **deterministic** implementation is a program. The same input gives the same output.
- A **nondeterministic** implementation is a language model or a person. A repeat call can give a
  different output, and the output can be wrong.

"Nondeterministic" here means only that. It is not the nondeterminism of automata theory or of
nondeterministic algorithms.

## One signature, two implementations

Both kinds of implementation share one **signature**: the type of the input, the type of the
output, and the effects the call may have. The signature does not say which kind of implementation
is behind it.

This is the point of the frame. The signature is where one implementation can replace the other.
Suppose a program can now do a task that a model did before. Bind the program to the same
signature. The callers do not change.

The separation of interface and implementation is an established idea in software design. What
this frame adds is one claim: the separation also holds when one implementation is a model or a
person.

Today, a model does many calls only because no program exists for them yet. A task is
**mechanical** when a program can already do it correctly. Mechanical work should go to the
program.

## The formats are the signatures

A format in this repository types one side of a call: its input, its output, or both. Typed calls
compose. A composition of typed calls is a pipeline of work.

This design requires a call to be typed before its result is checked, and before a program can
replace the model behind it. So, in this design, the formats come first.

## Acceptance checks what cannot be trusted by construction

A program's output can be trusted as far as the program is correct. A model's output cannot be
trusted that way: it can be wrong, and it can vary between runs.

So the result of a nondeterministic call is checked against the contract of the call. That check is
**acceptance**. In the terms of design by contract, acceptance is the postcondition of a call whose
implementation cannot be proved to meet it. The acceptance format, published separately, is the
record of that check: what was claimed, on what evidence, and what is still open.

One definition of a call can run many times. Each run needs its own acceptance check, because each
run can give a different result. Each run also needs its own record of what it did.

## Effects and state are typed too

The input and the output are not the whole call. Some formats in this family type the rest:

- a **grant** types the effects a call is allowed to have;
- an **obligation** types an effect a call is required to have, with a deadline and a test for
  "done";
- an **observation** types the record of what a call actually did.

## A runtime, in this design

In this design, a runtime executes the program. It schedules the calls. It checks that each input
and output matches its format. It calls the bound implementation. It checks the result, and it
records what happened. The design needs such a runtime. This repository does not contain one. It
contains the formats and their checkers.

## Example

A team translates release notes. The call is `translate(notes_en) -> notes_de`.

- The input format says: a Markdown file, a source language, a target language from a closed list.
- Today a model does the translation. Its output is checked: the file parses, every heading of the
  source has a counterpart, product names are unchanged. A person reads the rest.
- Later, a translation-memory program handles the recurring sentences. It is bound to the same
  signature. The callers do not notice. The checks stay the same.

## Where to go next

- [What a program can decide](02-what-a-program-can-decide.md): which parts of a typed result a
  program can check, field by field.
- [Class, instance, run](03-class-instance-run.md): the layers at which a call exists.
