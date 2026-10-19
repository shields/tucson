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

Curtains support position-only commands. The `zemismart-bcd300fb` profile uses
the observed protocol 3.3 mapping and correlated control acknowledgments; its
target-tracking exception and reconnect limitations are recorded in the
implementation plan and hardware acceptance record. Internet-blocked
qualification remains deferred, so production deployment is pending. Do not
generalize this mapping to other Zemismart devices. Synthetic curtain datapoints
belong only in tests. Do not add start/stop controls without a separate request.
