# Class, instance, run

*Status: a design. The names of the three layers are provisional. The narrowing between layers is
not checked by a program yet.*

## Three layers

Every managed piece of work exists at three layers. Each layer is a restriction of the layer above.

| layer | what it is | what is fixed here | what it produces |
|---|---|---|---|
| **class** | a definition: a template of the work | the shape only: which duties, triggers and dependencies apply, an intent template, a template of the permissions, the checks for acceptance | a definition document |
| **instance** | the class bound to concrete targets: these files, this repository, these permissions | the targets and the permissions | a bound contract of execution |
| **run** | one execution of one instance | the clock, the actual effects, the evidence | observation records and evidence, closed by an acceptance decision |

One class has many instances. One instance has many runs.

In programming terms: the class is a function definition. The instance is a closure, the function
with its environment bound. The run is one call. Binding a definition to part of its inputs ahead of
time is also the idea behind partial application and partial evaluation. The instance does the same
for targets and permissions.

## The call is the boundary

A class and an instance are at rest. They are durable, versioned files. An instance can be defined
in advance and stay registered for a long time. Example: a job that checks the links in one
repository, bound once, called on every change.

The **call** is the boundary between rest and execution. It sits between the instance and the run,
not inside the instance. A call is a trigger that fires. The run receives its permissions at the
moment of the call. Only the run executes.

A standing instance that fires many runs has the same shape as a standing duty that fires many
occurrences. The obligation format already describes that shape, so the layers reuse it.

## A lower layer never widens its parent

Each layer may narrow what the layer above allows, never widen it. The same rule appears in the
grant format, for permissions: a child grant is valid only if every permission it holds is covered
by its parent. This follows established practice in capability-based security, where a delegated
capability can be weakened but never strengthened. For the statement of direction that travels
with the work, the fields are prose, so the rule is stated, not checked.

## Example

- **Class:** "Check the external links in documentation." Constraints: read only; no requests to
  hosts on a deny list.
- **Instance:** the class bound to the `docs/` folder of one repository, with read access to that
  folder and network access to allowed hosts.
- **Run:** the call on Tuesday's change. It reports the broken links in `docs/`, read only. It is
  done when every link in `docs/` has a status of `ok`, `broken` or `unknown`.

## Other names on record

The layer names may change. Alternatives considered: definition, binding, execution; template,
instance, run; class, instance, session.
