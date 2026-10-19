# Project guidance

Follow [shields/right-answers](https://github.com/shields/right-answers) as the
canonical source for shared conventions. Read its current
[general guidance](https://github.com/shields/right-answers/blob/main/README.md),
[Python guide](https://github.com/shields/right-answers/blob/main/python.md),
[CI guide](https://github.com/shields/right-answers/blob/main/ci.md), and
[container guide](https://github.com/shields/right-answers/blob/main/containers.md),
including the shared configurations they reference, before implementation or
changes in those areas. Use the general prose and Markdown conventions for
documentation work.

For related Hoboken work, also follow the
[TypeScript guide](https://github.com/shields/right-answers/blob/main/typescript.md)
and read Hoboken's repository instructions before changing that repository.

The [implementation plan](docs/implementation-plan.md) records this project's
architecture and acceptance requirements. Use
[Rockville](https://github.com/shields/rockville) as the reference for bridge
behavior and operational quality; right-answers supplies the shared development
conventions. Keep project-specific decisions in this repository and reference
the canonical guides rather than maintaining a second copy of them.

Curtain infrastructure supports position-only commands. Keep Zemismart hardware
mappings deferred until the exact device is qualified; synthetic curtain
datapoints belong only in tests. The curtain contract is recorded in the
implementation plan. Do not add start/stop controls without a separate request.
