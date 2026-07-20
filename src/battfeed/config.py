"""Run configuration from a TOML file, plus the credential-hygiene helpers.

Long ``battfeed`` invocations carry a lot of state: an mc3000 slot and BLE
address, a dji folder path and keychain key, a push endpoint URL and bearer
token. Passing all of that as ``--opt KEY=VALUE`` flags is unwieldy and, worse,
**leaks secrets** into shell history and the process list (``ps`` / Task
Manager show a process's full argv to any local user). This module lets those
settings live in a file instead, and it centralises the redaction that keeps
secrets out of logs, error messages, the ``battfeed sources`` listing, and the
``.meta.json`` sidecars.

Redaction is two complementary layers, because neither alone is enough:

* **By key name** (:func:`is_secret_key` / :func:`redact_mapping`) -- a value
  whose *key* looks like a credential (``*key*`` / ``*token*`` / ``*secret*`` /
  ``*password*`` / ``*passphrase*`` / ``*credential*`` / ``*authorization*`` /
  ``*bearer*`` / ``*session*`` / ``*cookie*`` / ``*signature*`` / ``*private*``,
  and the short whole-segment ``auth`` / ``pat`` / ``pin`` / ``otp`` / ``salt``)
  is masked wherever it appears. The name is split into snake/kebab/camel
  segments so ``pat`` masks ``pat`` but not ``path``.
* **By value + URL** (:func:`redact_text` / :func:`mask_url_userinfo`) -- the
  concrete secret values resolved for a run are scrubbed from any text, and
  ``scheme://user:pass@host`` userinfo is masked, so a credential hiding under
  an innocuous key (``endpoint`` as a URL, a duplicated token under ``note``) is
  caught even though its key name is not suspicious.

Honest residuals (not claims of perfection): a secret a source reads from its
*own* env var without going through a registered ``(kwarg, env)`` mapping is
known only to that source (it must self-redact); a malformed-TOML *parse* error
can echo an inline secret before any value is resolved (so prefer ``${ENV:VAR}``,
which keeps the secret out of the file entirely); and a handler a source both
creates and logs to entirely within its own ``__init__`` is masked only if it
existed before construction.

Config file shape (TOML)
------------------------
::

    [collect]                      # defaults for `battfeed collect`
    interval = 2.0
    institution = "SINTEF"
    cell = "Pack-07"

    [import]                       # defaults for `battfeed import`
    interval = 5.0
    out_dir = "imported"
    watch = true

    [source.mc3000]                # options for `--source mc3000`
    slot = 1
    transport = "ble"
    address = "AA:BB:CC:DD:EE:FF"

    [source.dji]                   # options for `--source dji`
    type = "dji"                   # optional: the registered source *type*;
    path = "C:/logs/dji"           #   defaults to the block name ("dji" here),
    api_key = "${ENV:DJI_API_KEY}" #   so a block may alias a source under a
                                   #   different name, e.g. [source.bay2].

A ``[source.<name>]`` block matches ``--source <name>``. Its ``type`` field (if
absent, the block name) selects the registered source class; every other key is
a constructor keyword argument.

Precedence (explicit, and pinned by tests)
------------------------------------------
For a **source option** (a source constructor kwarg)::

    --opt KEY=VALUE   >   [source.<name>] in the config   >   the source's own
                                                              default

For a **run parameter** (``interval`` / ``institution`` / ``cell`` /
``duration`` / ``out`` / ``out_dir`` / ``watch``)::

    the CLI flag   >   [collect] or [import] in the config   >   the built-in
                                                                 default

There is no dedicated CLI flag for individual source kwargs -- ``--opt`` is that
layer -- so ``--opt`` overriding the config file is the source-kwarg spelling of
"the CLI wins". The environment enters through ``${ENV:VAR}`` expansion (below),
which is the sanctioned way to keep a secret out of the file itself; a few
sources also read their own env var (e.g. ``DJI_API_KEY``) as a last resort when
the kwarg is left unset, which sits at the "default" layer.

Environment expansion
---------------------
Any string value containing ``${ENV:VAR_NAME}`` is replaced with the value of
that environment variable **at load time**. A reference to an unset variable is
a hard error (better than silently sending an empty key on the wire). Write
``api_key = "${ENV:DJI_API_KEY}"`` and the secret never touches the file.

Python 3.10
-----------
Parsing TOML uses :mod:`tomllib`, which is standard library on **Python
3.11+**. battfeed supports 3.10, where ``tomllib`` does not exist, so the import
is conditional and falls back to the optional ``tomli`` backport if it happens
to be installed (no hard dependency is added to the core -- invariant I6). On
3.10 without ``tomli``, ``--config`` raises an actionable error pointing at the
upgrade or the one-line ``pip install tomli``; every other feature works
unchanged.
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "REDACTED",
    "Config",
    "ConfigError",
    "is_secret_key",
    "load_config",
    "mask_url_userinfo",
    "redact_mapping",
    "redact_text",
    "redact_value",
    "url_userinfo_passwords",
]


def _import_toml() -> Any:
    """Return a TOML module (``tomllib`` or ``tomli``), or ``None`` on 3.10.

    Imported by name via :func:`importlib.import_module` so a static type
    checker configured for Python 3.10 does not trip over ``tomllib`` (which
    only exists at runtime on 3.11+); the module is typed ``Any`` for the same
    reason. Adding no import of ``tomli`` at module top level keeps it a soft,
    optional backport rather than a core dependency (invariant I6).
    """
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    return None


#: The TOML parser (``tomllib`` on 3.11+, ``tomli`` if backported, else None).
_toml: Any = _import_toml()

#: The mask substituted for any secret value that would otherwise be echoed.
REDACTED = "***"

#: Secret tokens matched as a SUBSTRING of a name segment -- long and
#: unambiguous enough that an incidental hit is implausible (``key`` catches
#: ``apikey``; ``authorization`` catches itself; ``password`` catches
#: ``db_password``).
_SUBSTRING_SECRETS: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "credential",
    "authorization",
    "authentication",
    "signature",
    "session",
    "cookie",
    "private",
    "bearer",
)

#: Secret tokens matched ONLY as a WHOLE name segment -- too short/ambiguous to
#: use as substrings (``pat`` must not fire on ``path``/``compatible``, ``pin``
#: not on ``spinner``, ``salt`` not on ``asphalt``, ``auth`` not on ``authors``).
_WHOLE_SEGMENT_SECRETS: frozenset[str] = frozenset(
    {"auth", "pass", "cred", "creds", "pat", "pin", "otp", "sig", "salt"}
)

#: camelCase boundary, so ``accessToken`` splits like ``access_token``.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: A URL carrying ``user[:pass]@`` userinfo, for masking embedded credentials
#: (generalises ``HttpPushSink``'s per-URL redaction to arbitrary text).
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)([^/@\s]+)@")

#: ``${ENV:VAR_NAME}`` reference, expanded from the environment at load time.
_ENV_REF_RE = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")

#: Run-parameter keys accepted in the [collect] / [import] tables (typos in
#: anything else are reported rather than silently ignored).
_COLLECT_KEYS = frozenset({"interval", "institution", "cell", "duration", "out"})
_IMPORT_KEYS = frozenset({"interval", "institution", "out_dir", "watch"})


class ConfigError(ValueError):
    """A config file is missing, unparseable, or structurally invalid.

    Subclasses :class:`ValueError` so the CLI's existing actionable-error
    handling surfaces the message without a traceback.
    """


# -- credential hygiene ------------------------------------------------------


def _name_segments(name: str) -> list[str]:
    """Split a name into lowercase word segments on ``_``/``-``/digits/camelCase."""
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", name)
    return [seg for seg in re.split(r"[^A-Za-z]+", spaced.lower()) if seg]


def is_secret_key(name: str) -> bool:
    """True if an option/metadata key name looks like it holds a secret.

    The name is split into word segments (on ``_`` / ``-`` / digits / camelCase);
    a segment matches if it *equals* one of the short whole-segment secrets
    (``auth``/``pass``/``cred``/``pat``/``pin``/``otp``/``sig``/``salt``) or
    *contains* one of the longer unambiguous ones (``key``/``token``/``secret``/
    ``password``/``passphrase``/``credential``/``authorization``/``signature``/
    ``session``/``cookie``/``private``/``bearer`` and kin). This masks
    ``authorization`` / ``bearer_token`` / ``db_password`` / ``private_key`` /
    ``session_cookie`` / ``passphrase`` while leaving ``path`` / ``format`` /
    ``transport`` / ``compatibility`` / ``interval`` / ``institution`` alone --
    the segment split is what keeps ``pat`` off ``path`` and ``salt`` off
    ``asphalt``.
    """
    for segment in _name_segments(name):
        if segment in _WHOLE_SEGMENT_SECRETS:
            return True
        if any(token in segment for token in _SUBSTRING_SECRETS):
            return True
    return False


def redact_value(name: str, value: Any) -> Any:
    """Return ``value`` unless ``name`` is a secret key, then :data:`REDACTED`."""
    return REDACTED if is_secret_key(name) else value


def mask_url_userinfo(text: str) -> str:
    """Mask ``user:pass@`` userinfo in every ``scheme://user:pass@host`` in ``text``.

    ``https://u:SECRET@host/x`` -> ``https://***@host/x``. Generalises
    ``HttpPushSink``'s per-URL redaction to free text (a log line, an error, a
    metadata value) where a credential may hide under a non-secret key name.
    """
    return _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}***@", text)


def url_userinfo_passwords(text: str) -> list[str]:
    """Return the password component of every ``scheme://user:pass@`` in ``text``.

    Harvested into the run's secret-value set so a credential embedded in a URL
    under an innocuous key (``endpoint``, ``dsn``) is scrubbed by value from
    logs and errors too, not only masked in place.
    """
    out: list[str] = []
    for match in _URL_USERINFO_RE.finditer(text):
        userinfo = match.group(2)
        if ":" in userinfo:
            out.append(userinfo.split(":", 1)[1])
    return out


def redact_text(text: str, secrets: Iterable[Any] = ()) -> str:
    """Scrub known secret *values* from ``text`` and mask any URL userinfo.

    Two value-based layers that complement the key-name masking of
    :func:`redact_mapping`: every literal in ``secrets`` (longest first, so a
    secret that contains another is fully masked) becomes ``***``, and any
    ``scheme://user:pass@host`` credential is masked even when its value was
    never a named option. Safe on empty inputs.
    """
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    return mask_url_userinfo(text)


def redact_mapping(mapping: Mapping[str, Any], secrets: Iterable[Any] = ()) -> dict[str, Any]:
    """Deep-copy ``mapping`` with secrets masked by BOTH key name and value.

    Key-name layer: any value whose key :func:`is_secret_key` becomes ``***``.
    Value layer (applied to every remaining string, at any depth via
    :func:`redact_text`): known secret *values* in ``secrets`` are scrubbed and
    URL userinfo is masked -- so a credential hiding under an innocuous key (an
    ``endpoint`` URL, a copy of the token under ``note``) is caught too. Recurses
    through nested mappings, lists and tuples; the input is never mutated. This
    is the pass used at the ``.meta.json`` sidecar boundary.
    """
    secret_values = [str(s) for s in secrets if s]

    def _walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: (REDACTED if is_secret_key(str(key)) else _walk(val))
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_walk(item) for item in value]
        if isinstance(value, str):
            return redact_text(value, secret_values)
        return value

    return {
        key: (REDACTED if is_secret_key(str(key)) else _walk(val)) for key, val in mapping.items()
    }


# -- config loading ----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """A parsed battfeed config file; ``${ENV:VAR}`` is expanded lazily on access.

    ``collect`` / ``import_`` hold the raw run-parameter tables; ``sources``
    maps each ``[source.<name>]`` block name to its raw ``{type?, **kwargs}``
    dict. Expansion happens only in :meth:`run_params` / :meth:`source_block`,
    when a section is actually consumed -- so an unset ``${ENV:VAR}`` in a
    block the run does not use (a different source, or the other verb's
    section) never derails the run. Each accessor returns a fresh copy.
    """

    collect: dict[str, Any] = field(default_factory=dict)
    import_: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def run_params(self, verb: str) -> dict[str, Any]:
        """Return the env-expanded run-parameter table for ``collect``/``import``."""
        section = self.collect if verb == "collect" else self.import_
        return _expand_env(dict(section))

    def source_block(self, name: str) -> dict[str, Any] | None:
        """Return the env-expanded ``[source.<name>]`` block, or ``None``."""
        block = self.sources.get(name)
        return _expand_env(dict(block)) if block is not None else None


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${ENV:VAR}`` in every string within ``value``."""
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            try:
                return os.environ[var]
            except KeyError:
                raise ConfigError(
                    f"config references ${{ENV:{var}}} but the environment variable "
                    f"{var!r} is not set"
                ) from None

        return _ENV_REF_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {key: _expand_env(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _validate_keys(table: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) {unknown} in [{where}]; allowed keys are {sorted(allowed)}"
        )


def load_config(path: str | Path) -> Config:
    """Load and validate the TOML config at ``path`` (env expansion is lazy).

    ``${ENV:VAR}`` references are resolved when a section is read via
    :meth:`Config.run_params` / :meth:`Config.source_block`, not here, so an
    unset variable in an unused block does not break an unrelated run.

    Raises:
        ConfigError: if the file is missing or unreadable, is not valid TOML,
            has a malformed ``[collect]`` / ``[import]`` / ``[source.*]``
            section, or is given while no TOML parser is available (Python 3.10
            without ``tomli``). An unset ``${ENV:VAR}`` is reported later, when
            the section that references it is used. Every message is actionable
            on its own -- the CLI prints it without a traceback.
    """
    if _toml is None:  # pragma: no cover - only on a 3.10 interpreter without tomli
        raise ConfigError(
            "--config needs a TOML parser. tomllib is standard library on "
            "Python 3.11+, so upgrade the interpreter, or on 3.10 install the "
            "backport with: pip install tomli."
        )
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {str(path)!r}: {exc}") from exc
    try:
        data = _toml.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file {str(path)!r} is not valid UTF-8: {exc}") from exc
    except Exception as exc:  # tomllib raises TOMLDecodeError; keep it version-agnostic
        raise ConfigError(f"config file {str(path)!r} is not valid TOML: {exc}") from exc

    collect = data.get("collect", {})
    imports = data.get("import", {})
    sources_raw = data.get("source", {})
    for label, table in (("collect", collect), ("import", imports), ("source", sources_raw)):
        if not isinstance(table, dict):
            raise ConfigError(f"[{label}] must be a table, got {type(table).__name__}")
    _validate_keys(collect, _COLLECT_KEYS, "collect")
    _validate_keys(imports, _IMPORT_KEYS, "import")

    sources: dict[str, dict[str, Any]] = {}
    for name, block in sources_raw.items():
        if not isinstance(block, dict):
            raise ConfigError(f"[source.{name}] must be a table, got {type(block).__name__}")
        sources[name] = dict(block)  # raw; env-expanded lazily on access

    return Config(collect=dict(collect), import_=dict(imports), sources=sources)
