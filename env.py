"""What an agent -- and a gate -- are allowed to see of the host environment.

`Run.env` used to be `dict(os.environ, PYTHONPYCACHEPREFIX=...)`: every
variable the operator's shell happened to be carrying was handed to the agent
process, and through the agent's bash tool to everything it chose to run. On a
developer's machine that is a cloud session token, a registry password, a
database URL and whatever the last `source .env` left behind. None of it is
needed to edit files in a worktree, and an agent that runs `printenv` for its
own reasons -- or a gate command, which is a shell string from config -- can
read all of it and commit it.

So the default is an allowlist. Three things decide what survives:

* `BASE_ALLOW` below: what any process needs to run at all (a PATH, a HOME, a
  locale, a temp dir, TLS trust, proxies) plus the toolchain variables a gate
  or an agent's bash tool routinely needs. Broad on purpose -- this is the set
  that keeps ordinary builds working, and a build that breaks for a missing
  variable is a worse failure than it looks, because it breaks inside an
  unattended run and reads as the model's fault.
* the harness adapter's own `env_passthrough`, so `PI_*` reaches pi and
  `OMP_*` reaches omp without either name being known here. `PI_CODING_AGENT_DIR`
  arrives this way, which matters: it relocates pi's entire config directory
  and is how a run is pointed at a scratch config at all.
* `[env] pass` in config, for the ones only the operator can know.

On top of that, a name that *looks* like a credential is dropped even when a
prefix rule would have allowed it, because prefix rules are blunt in exactly
the wrong direction: `NODE_*` is a reasonable thing to pass and
`NODE_AUTH_TOKEN` is a registry credential; `npm_config_*` is ordinary
configuration and `npm_config__auth` is a password. Listing a name in
`[env] pass` is an explicit decision and overrides that filter -- which is how
`ANTHROPIC_API_KEY` gets through when a run genuinely needs it in the
environment rather than in the harness's own config file.  It has to be the
exact name: a `pass` entry ending in `*` still adds variables, but it never
exempts a credential, because nobody typing `AWS_*` for `AWS_REGION` means to
include the secret key.

`inherit = "all"` restores the old behaviour wholesale, for the operator who
has looked at this and wants their whole environment anyway.

Everything here is a pure function of its arguments -- no `os.environ`, no
config loading -- so the policy can be tested without a worktree or a harness.
"""

from __future__ import annotations

# Enough to run a process, find its tools, and talk to the network.  Trailing
# `*` is a prefix; everything else is an exact name.
BASE_ALLOW: tuple[str, ...] = (
    # who and where
    "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD",
    "PATH", "TMPDIR", "TEMP", "TMP",
    # locale, time, terminal
    "LANG", "LANGUAGE", "LC_*", "TZ",
    "TERM", "TERMINFO", "COLORTERM", "NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE",
    "COLUMNS", "LINES",
    # the freedesktop dirs, which is where most tools keep their caches
    "XDG_*",
    # TLS trust: a run behind a corporate root that cannot see it fails every
    # request the agent makes, with no clue as to why.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_PATH",
    # proxies, in both the spellings the world actually uses
    "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy",
    # toolchains.  A gate is somebody's real build command; these are the
    # variables that build reads without anyone thinking about it.
    "CARGO_HOME", "RUSTUP_HOME", "RUST_BACKTRACE",
    "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "GOFLAGS", "GOPROXY",
    "JAVA_HOME", "JDK_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_USER_HOME",
    "NODE_*", "NVM_*", "PNPM_HOME", "COREPACK_*", "YARN_*", "npm_config_*",
    "PYTHON*", "PYENV_*", "VIRTUAL_ENV", "PIP_*", "UV_*", "CONDA_*", "POETRY_*",
    "DOTNET_*", "NUGET_*",
    "RUBYOPT", "GEM_HOME", "GEM_PATH", "BUNDLE_*",
    "HOMEBREW_*", "MAKEFLAGS", "CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS",
    "PKG_CONFIG_PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "CI", "DEBIAN_FRONTEND",
    # Windows needs these to have a working process at all.
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
)

# Substrings that make a name look like a credential.  Matched case-insensitively
# against the whole name, and applied *after* the allow rules, so a blunt prefix
# cannot let one through by accident.  Listing a name in `[env] pass` is an
# explicit decision and beats this.
SECRET_MARKERS: tuple[str, ...] = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE",
    "APIKEY", "API_KEY", "ACCESS_KEY", "PRIVATE_KEY", "SECRET_KEY", "SIGNING_KEY",
    "CREDENTIAL", "AUTH", "SESSION", "COOKIE", "SIGNATURE",
    "LICENSE_KEY", "SERIAL", "SALT", "CIPHER",
)

REDACTED = "<redacted>"


def looks_secret(name: str) -> bool:
    """Does this variable's *name* suggest it carries a credential?

    Names only.  Values are never inspected: a heuristic over values would
    have to read them to decide, and the whole point is to handle these as
    little as possible.
    """
    upper = name.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def matches(name: str, patterns) -> bool:
    """Exact name, or a trailing-`*` prefix."""
    for pattern in patterns:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


def build(
    environ: dict,
    *,
    inherit: str = "allowlist",
    harness_names: tuple[str, ...] = (),
    allow: tuple[str, ...] = (),
    block: tuple[str, ...] = (),
    overrides: dict | None = None,
) -> dict:
    """The environment a child process should actually get.

    `allow` is the operator's `[env] pass`.  It adds names by exact match or by
    prefix, and an entry that is an *exact* name also exempts that name from
    the credential filter -- a prefix does not, so `AWS_*` cannot hand over a
    secret key that nobody asked for by name.  `block` is `[env] block` and
    wins over everything, so a variable can always be kept out no matter what
    allowed it.  `overrides` are the loop's own additions and are never
    filtered; they are not inherited from anywhere.
    """
    overrides = overrides or {}
    if inherit == "all":
        kept = dict(environ)
    else:
        patterns = tuple(BASE_ALLOW) + tuple(harness_names) + tuple(allow)
        kept = {}
        for name, value in environ.items():
            if not matches(name, patterns):
                continue
            # Naming a credential is a decision; matching a prefix is not.
            # `pass = ["AWS_*"]` from someone who wanted `AWS_REGION` must not
            # quietly hand over `AWS_SECRET_ACCESS_KEY` as well, so the
            # exemption takes the exact name and nothing else.
            if looks_secret(name) and name not in allow:
                continue
            kept[name] = value
    for name in list(kept):
        if matches(name, block):
            del kept[name]
    kept.update(overrides)
    return kept


def withheld(environ: dict, passed: dict) -> list[str]:
    """Credential-shaped names on the host that the child will not see.

    For one line at the start of a run.  A harness that needed one of these
    fails somewhere far away from the cause -- an auth error mid-iteration, or
    a provider that simply returns nothing -- and the operator has no reason to
    connect it to a setting they never set.  Naming them costs one line and
    turns that into a five-second fix.  The names only: never the values.
    """
    return sorted(
        name for name in environ
        if name not in passed and looks_secret(name)
    )


def redact(env: dict) -> dict:
    """The same mapping with credential-shaped values masked, for diagnostics.

    Anything written to an event log, a status file or a terminal goes through
    this.  A run's own record is read by whoever is debugging it, which is not
    always the person whose machine it ran on.
    """
    return {
        name: (REDACTED if looks_secret(name) else value)
        for name, value in env.items()
    }
