# Design principles

battfeed's design is a handful of deliberate rules. Knowing them makes the package's behaviour, and its refusals, predictable.

## One job: acquisition

battfeed gets battery readings out of a device and into a clean, shared file format. That is the whole job, and the boundaries are policed:

- **No vendor-file normalization.** Parsing and harmonising exported Neware / BioLogic / Digatron / Basytec files belongs to [batterydf](https://github.com/battery-data-alliance) (Battery Data Alliance). This is why `CsvTailSource` demands an explicit column map and will never guess synonyms: the day battfeed starts guessing, it has become a normalizer.
- **No hosted services.** `HttpPushSink` is a one-way POST to an endpoint you point it at — a *sink*, not a server. battfeed runs no server and manages no fleet or tenants; publishing and sharing belong to registry tooling.
- **No twin or model logic.** State estimation and twin orchestration live in [battwin](https://github.com/DigiBatt/battwin), which consumes battfeed's files.

The same lines appear from the other side in battwin's own non-goals — the stack is a set of small tools that compose by file format, not a platform.

## BDF is the only output

Everything battfeed writes is BDF: same column names, same naming pattern, same sign convention, same sidecar, whatever the source. One output contract means everything downstream — batterydf, battwin, registries, your own pandas scripts — reads battfeed output without knowing which of a hundred devices produced it. The contract is specified in the [output reference](../reference/output.md).

## The seam is structural

`DataSource` and `Sink` are `typing.Protocol`s: your source imports **nothing** from battfeed. That is a deliberate inversion of the usual plugin relationship — battfeed depends on the *shape* of your class, never the reverse — and it means a vendor can add battfeed support to their own package without taking a dependency, and their source keeps working as battfeed evolves. The entry-point registry (`battfeed.sources`) makes such sources first-class citizens of the CLI, and battfeed's own built-ins register through it, so the mechanism cannot rot.

**Importers are just sources.** Reading a folder of logged files uses the same `DataSource` contract as polling a live device; `battfeed import` is a drain loop, not a second architecture. One seam to learn, one contract test (`check_source`) to pass.

## The core stays dependency-free

A bare `pip install battfeed` brings zero third-party packages; optional libraries are imported only when the source or sink that needs them actually runs. Acquisition code ends up on lab PCs, gateway boxes, and locked-down industrial hosts where every dependency is friction and a supply-chain question. The costs of this rule are real (stdlib HTTP in `HttpPushSink`, WMI via an optional extra) and accepted; CI runs a no-extras leg on every platform precisely to catch an accidental hard import.

## Sources raise; the harvester decides

A source that cannot reach its device raises. The `Harvester` owns retry — exponential backoff under a configurable `ErrorPolicy`, abandoning the run only after too many consecutive failures. Sources implementing their own retry loops would hide exactly the signal the policy needs, so the rule is firm, and it is what [reliability](reliability.md) builds on.

## Names tell the truth

`battfeed sources` marks a source that cannot run here as `[unavailable: reason]` instead of hiding it; `--opt address=auto` resolves a single unambiguous device or fails with the candidate list, never guessing silently; exit code 2 means "your configuration is wrong, restarting will not help." Small choices, one principle: the operator should never have to infer what the tool decided.
