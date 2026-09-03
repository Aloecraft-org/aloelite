//! The command line as data, and the parser over it.
//!
//! [`VERBS`] and [`GLOBALS`] are the contract (`aloelite/config/cli.yaml`)
//! written as Rust: `tests/contract.rs` holds them against the YAML in both
//! directions. The parser reads only these tables, so a verb or a flag that
//! is not in the contract cannot be parsed, and one that is cannot be
//! forgotten. Help text is generated from the same tables.
//!
//! Shape, as argparse has it: global options come BEFORE the verb; the first
//! bare token is the verb; a verb's own flags and positionals follow it.
//! `--pin` takes the next token as its value unless that token starts with
//! `-` (or there is none), in which case it is a bare `--pin` and prompts.

use std::collections::BTreeSet;

use crate::pin::PinArg;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Whether a verb needs a session mount (`-v` resolution, PIN) or works on
/// the file alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Mount,
    File,
}

#[derive(Debug)]
pub struct Flag {
    pub name: &'static str,
    pub opts: &'static [&'static str],
}

#[derive(Debug)]
pub struct Positional {
    pub name: &'static str,
    pub optional: bool,
}

#[derive(Debug)]
pub struct Sub {
    pub name: &'static str,
    pub args: &'static [Positional],
}

#[derive(Debug)]
pub struct Verb {
    pub name: &'static str,
    pub scope: Scope,
    pub args: &'static [Positional],
    pub flags: &'static [Flag],
    pub sub: &'static [Sub],
    pub help: &'static str,
}

const fn req(name: &'static str) -> Positional {
    Positional {
        name,
        optional: false,
    }
}

const fn opt(name: &'static str) -> Positional {
    Positional {
        name,
        optional: true,
    }
}

/// Every verb, in the contract's order.
pub const VERBS: &[Verb] = &[
    Verb {
        name: "ls",
        scope: Scope::Mount,
        args: &[opt("path")],
        flags: &[Flag {
            name: "long",
            opts: &["-l", "--long"],
        }],
        sub: &[],
        help: "list a directory",
    },
    Verb {
        name: "put",
        scope: Scope::Mount,
        args: &[req("src"), req("dst")],
        flags: &[
            Flag {
                name: "append",
                opts: &["--append"],
            },
            Flag {
                name: "recursive",
                opts: &["-r", "--recursive"],
            },
        ],
        sub: &[],
        help: "write a local file (or '-' = stdin) to a path",
    },
    Verb {
        name: "get",
        scope: Scope::Mount,
        args: &[req("src"), opt("dst")],
        flags: &[Flag {
            name: "recursive",
            opts: &["-r", "--recursive"],
        }],
        sub: &[],
        help: "read a path to a local file (or '-' = stdout)",
    },
    Verb {
        name: "cat",
        scope: Scope::Mount,
        args: &[req("path")],
        flags: &[],
        sub: &[],
        help: "print a file to stdout",
    },
    Verb {
        name: "cp",
        scope: Scope::Mount,
        args: &[req("src"), req("dst")],
        flags: &[],
        sub: &[],
        help: "copy (dedup-preserving, near-free)",
    },
    Verb {
        name: "stat",
        scope: Scope::Mount,
        args: &[req("path")],
        flags: &[],
        sub: &[],
        help: "show a node's details",
    },
    Verb {
        name: "tree",
        scope: Scope::Mount,
        args: &[opt("path")],
        flags: &[],
        sub: &[],
        help: "print a directory tree",
    },
    Verb {
        name: "mkdir",
        scope: Scope::Mount,
        args: &[req("path")],
        flags: &[Flag {
            name: "parents",
            opts: &["-p", "--parents"],
        }],
        sub: &[],
        help: "create a container (-p: parents, no error if it exists)",
    },
    Verb {
        name: "rm",
        scope: Scope::Mount,
        args: &[req("path")],
        flags: &[Flag {
            name: "recursive",
            opts: &["-r", "--recursive"],
        }],
        sub: &[],
        help: "remove an entry or empty container (-r: a tree)",
    },
    Verb {
        name: "mv",
        scope: Scope::Mount,
        args: &[req("src"), req("dst")],
        flags: &[],
        sub: &[],
        help: "move/rename",
    },
    Verb {
        name: "volumes",
        scope: Scope::File,
        args: &[],
        flags: &[],
        sub: &[],
        help: "list volumes in the file",
    },
    Verb {
        name: "volume",
        scope: Scope::File,
        args: &[],
        flags: &[],
        sub: &[
            Sub {
                name: "ls",
                args: &[],
            },
            Sub {
                name: "create",
                args: &[req("name")],
            },
        ],
        help: "manage volumes: ls, create NAME (encrypted iff a --pin* flag is given)",
    },
    Verb {
        name: "mounts",
        scope: Scope::File,
        args: &[],
        flags: &[Flag {
            name: "all",
            opts: &["--all"],
        }],
        sub: &[],
        help: "list durable mounts in the file (--all: include retired)",
    },
    Verb {
        name: "prune",
        scope: Scope::File,
        args: &[],
        flags: &[Flag {
            name: "vacuum",
            opts: &["--vacuum"],
        }],
        sub: &[],
        help: "reclaim unreferenced nodes, locks, and content (--vacuum: compact)",
    },
    Verb {
        name: "pin",
        scope: Scope::File,
        args: &[],
        flags: &[],
        sub: &[Sub {
            name: "check",
            args: &[],
        }],
        help: "PIN utilities: check (verify a PIN against the volume, exit 0/1)",
    },
];

#[derive(Debug)]
pub struct Global {
    pub name: &'static str,
    pub opts: &'static [&'static str],
    /// The value's placeholder, or `None` for a bare flag.
    pub value: Option<&'static str>,
    /// The value may be omitted (only `--pin`, which then prompts).
    pub optional_value: bool,
    pub help: &'static str,
}

/// Options accepted before the verb, in the contract's order.
pub const GLOBALS: &[Global] = &[
    Global {
        name: "file",
        opts: &["-f", "--file"],
        value: Some("PATH"),
        optional_value: false,
        help: "path to the .sqlite/.fs file (default: $ALOELITE_FILE)",
    },
    Global {
        name: "volume",
        opts: &["-v", "--volume"],
        value: Some("NAME_OR_ID"),
        optional_value: false,
        help: "volume name or id (optional if the file has exactly one)",
    },
    Global {
        name: "in",
        opts: &["-i", "--in"],
        value: Some("PATH"),
        optional_value: false,
        help: "write stdin to PATH (create/overwrite); a terminal stdin writes nothing",
    },
    Global {
        name: "append",
        opts: &["-a", "--append"],
        value: Some("PATH"),
        optional_value: false,
        help: "append stdin to PATH (creates it)",
    },
    Global {
        name: "pin",
        opts: &["--pin"],
        value: Some("SECRET"),
        optional_value: true,
        help: "PIN; bare --pin prompts on the terminal (place it before another flag)",
    },
    Global {
        name: "pin_file",
        opts: &["--pin-file"],
        value: Some("PATH"),
        optional_value: false,
        help: "file whose contents are the PIN",
    },
    Global {
        name: "pin_env",
        opts: &["--pin-env"],
        value: Some("VAR"),
        optional_value: false,
        help: "environment variable holding the PIN",
    },
    Global {
        name: "version",
        opts: &["--version"],
        value: None,
        optional_value: false,
        help: "print the version and exit",
    },
];

/// First tokens the Python command hands to sibling programs; this one
/// names the right command instead (see the contract's `delegations`).
pub const DELEGATIONS: &[&str] = &["fuse", "web", "admin"];

/// The parsed global options.
#[derive(Debug, Default)]
pub struct Globals {
    pub file: Option<String>,
    pub volume: Option<String>,
    pub in_path: Option<String>,
    pub append_path: Option<String>,
    pub pin: Option<PinArg>,
    pub pin_file: Option<String>,
    pub pin_env: Option<String>,
}

impl Globals {
    /// Whether any PIN source was named.
    pub fn any_pin_flag(&self) -> bool {
        self.pin.is_some() || self.pin_file.is_some() || self.pin_env.is_some()
    }
}

/// A parsed verb with its arguments.
#[derive(Debug)]
pub struct Command {
    pub verb: &'static Verb,
    pub sub: Option<&'static Sub>,
    args: Vec<(&'static str, String)>,
    flags: BTreeSet<&'static str>,
}

impl Command {
    pub fn arg(&self, name: &str) -> Option<&str> {
        self.args
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, v)| v.as_str())
    }

    pub fn flag(&self, name: &str) -> bool {
        self.flags.contains(name)
    }
}

/// What a command line asks for.
// Built once per process; boxing the one large variant would buy nothing.
#[allow(clippy::large_enum_variant)]
#[derive(Debug)]
pub enum Outcome {
    Run(Globals, Option<Command>),
    Help,
    Version,
    Delegated(String),
}

/// Parse `argv` (without the program name). `Err` is a usage message: the
/// caller prints it with the usage line and exits 2.
pub fn parse(argv: &[String]) -> Result<Outcome, String> {
    if let Some(first) = argv.first()
        && DELEGATIONS.contains(&first.as_str())
    {
        return Ok(Outcome::Delegated(first.clone()));
    }
    let mut g = Globals {
        file: std::env::var("ALOELITE_FILE")
            .ok()
            .filter(|s| !s.is_empty()),
        ..Default::default()
    };
    let mut i = 0;
    let verb_at = loop {
        let Some(tok) = argv.get(i) else {
            break None;
        };
        if tok == "-h" || tok == "--help" {
            return Ok(Outcome::Help);
        }
        if tok == "--" {
            // end of options, as argparse has it: the next token is the verb
            i += 1;
            break argv.get(i).map(|_| i);
        }
        if !tok.starts_with('-') || tok == "-" {
            break Some(i);
        }
        let (flag, inline) = split_inline(tok);
        let global = GLOBALS
            .iter()
            .find(|gl| gl.opts.contains(&flag))
            .ok_or_else(|| format!("unrecognized arguments: {tok}"))?;
        i += 1;
        let value = match (global.value, inline) {
            (None, Some(_)) => return Err(format!("{flag} takes no value")),
            (None, None) => None,
            (Some(_), Some(v)) => Some(v.to_owned()),
            (Some(_), None) => {
                let next = argv
                    .get(i)
                    .filter(|n| !n.starts_with('-') || n.as_str() == "-");
                match next {
                    Some(v) => {
                        i += 1;
                        Some(v.clone())
                    }
                    None if global.optional_value => None,
                    None => return Err(format!("{flag}: expected one argument")),
                }
            }
        };
        match global.name {
            "file" => g.file = value,
            "volume" => g.volume = value,
            "in" => g.in_path = value,
            "append" => g.append_path = value,
            "pin" => g.pin = Some(value.map_or(PinArg::Prompt, PinArg::Value)),
            "pin_file" => g.pin_file = value,
            "pin_env" => g.pin_env = value,
            "version" => return Ok(Outcome::Version),
            _ => unreachable!("every global is matched"),
        }
    };
    let Some(at) = verb_at else {
        return Ok(Outcome::Run(g, None));
    };
    let verb = VERBS.iter().find(|v| v.name == argv[at]).ok_or_else(|| {
        let names: Vec<&str> = VERBS.iter().map(|v| v.name).collect();
        format!(
            "invalid choice: {:?} (choose from {})",
            argv[at],
            names.join(", ")
        )
    })?;
    let command = parse_verb(verb, &argv[at + 1..])?;
    Ok(Outcome::Run(g, Some(command)))
}

/// The help text, from the tables.
pub fn usage() -> String {
    let mut s = String::from(
        "usage: aloelite [-f FILE] [-v NAME_OR_ID] [--pin [SECRET] | --pin-file PATH | --pin-env VAR]\n\
         \x20               [-i PATH | -a PATH] [--version] [VERB ...]\n\n\
         Operate on an Aloelite filesystem file. With no VERB, create the file\n\
         (with a default volume) or show what it holds.\n\nverbs:\n",
    );
    for v in VERBS {
        s.push_str(&format!("  {:<8} {}\n", v.name, v.help));
    }
    s.push_str("\noptions (before the verb):\n");
    for gl in GLOBALS {
        let spec = match (gl.value, gl.optional_value) {
            (Some(v), true) => format!("{} [{v}]", gl.opts.join(", ")),
            (Some(v), false) => format!("{} {v}", gl.opts.join(", ")),
            (None, _) => gl.opts.join(", "),
        };
        s.push_str(&format!("  {spec:<24} {}\n", gl.help));
    }
    s.push_str("  -h, --help               show this help and exit\n");
    s
}

// ---------------------------------------------------------------------------
// depth: a verb's own arguments
// ---------------------------------------------------------------------------

fn parse_verb(verb: &'static Verb, rest: &[String]) -> Result<Command, String> {
    let mut flags = BTreeSet::new();
    let mut positionals: Vec<String> = Vec::new();
    let mut only_positionals = false;
    for tok in rest {
        if only_positionals {
            positionals.push(tok.clone());
            continue;
        }
        if tok == "--" {
            only_positionals = true;
            continue;
        }
        if tok == "-h" || tok == "--help" {
            return Err(format!("{}: {}", verb.name, verb.help));
        }
        if tok.starts_with('-') && tok != "-" {
            let flag = verb
                .flags
                .iter()
                .find(|f| f.opts.contains(&tok.as_str()))
                .ok_or_else(|| format!("{}: unrecognized arguments: {tok}", verb.name))?;
            flags.insert(flag.name);
        } else {
            positionals.push(tok.clone());
        }
    }
    let (sub, shape): (Option<&'static Sub>, &'static [Positional]) = if verb.sub.is_empty() {
        (None, verb.args)
    } else {
        if positionals.is_empty() {
            let names: Vec<&str> = verb.sub.iter().map(|s| s.name).collect();
            return Err(format!(
                "{}: a sub-command is required ({})",
                verb.name,
                names.join(", ")
            ));
        }
        let name = positionals.remove(0);
        let sub = verb.sub.iter().find(|s| s.name == name).ok_or_else(|| {
            let names: Vec<&str> = verb.sub.iter().map(|s| s.name).collect();
            format!(
                "{}: invalid choice: {name:?} (choose from {})",
                verb.name,
                names.join(", ")
            )
        })?;
        (Some(sub), sub.args)
    };
    let required = shape.iter().filter(|p| !p.optional).count();
    if positionals.len() < required {
        let missing: Vec<&str> = shape[positionals.len()..]
            .iter()
            .filter(|p| !p.optional)
            .map(|p| p.name)
            .collect();
        return Err(format!(
            "{}: the following arguments are required: {}",
            verb.name,
            missing.join(", ")
        ));
    }
    if positionals.len() > shape.len() {
        return Err(format!(
            "{}: unrecognized arguments: {}",
            verb.name,
            positionals[shape.len()..].join(" ")
        ));
    }
    let args = shape
        .iter()
        .zip(positionals)
        .map(|(p, v)| (p.name, v))
        .collect();
    Ok(Command {
        verb,
        sub,
        args,
        flags,
    })
}

/// `--file=PATH` → (`--file`, Some(`PATH`)); short options never inline.
fn split_inline(tok: &str) -> (&str, Option<&str>) {
    if tok.starts_with("--")
        && let Some((flag, value)) = tok.split_once('=')
    {
        return (flag, Some(value));
    }
    (tok, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(args: &[&str]) -> Result<Outcome, String> {
        parse(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>())
    }

    fn run(args: &[&str]) -> (Globals, Option<Command>) {
        match p(args) {
            Ok(Outcome::Run(g, c)) => (g, c),
            other => panic!("expected Run, got {other:?}"),
        }
    }

    #[test]
    fn globals_then_verb_then_verb_flags_and_positionals() {
        let (g, c) = run(&[
            "-f", "x.fs", "-v", "vol", "put", "-r", "./src", "/dst", "--append",
        ]);
        assert_eq!(g.file.as_deref(), Some("x.fs"));
        assert_eq!(g.volume.as_deref(), Some("vol"));
        let c = c.unwrap();
        assert_eq!(c.verb.name, "put");
        assert!(c.flag("recursive") && c.flag("append"));
        assert_eq!(c.arg("src"), Some("./src"));
        assert_eq!(c.arg("dst"), Some("/dst"));
    }

    #[test]
    fn optional_positionals_and_sub_verbs() {
        let (_, c) = run(&["-f", "x.fs", "ls"]);
        assert_eq!(c.unwrap().arg("path"), None);
        let (_, c) = run(&["-f", "x.fs", "volume", "create", "vault"]);
        let c = c.unwrap();
        assert_eq!(c.sub.unwrap().name, "create");
        assert_eq!(c.arg("name"), Some("vault"));
        assert!(
            p(&["-f", "x.fs", "volume"]).is_err(),
            "a sub-verb is required"
        );
        assert!(p(&["-f", "x.fs", "pin", "nope"]).is_err());
    }

    #[test]
    fn bare_pin_versus_valued_pin() {
        let (g, _) = run(&["--pin", "-f", "x.fs", "ls"]);
        assert!(matches!(g.pin, Some(PinArg::Prompt)));
        let (g, _) = run(&["--pin", "secret", "-f", "x.fs", "ls"]);
        assert!(matches!(g.pin, Some(PinArg::Value(ref v)) if v == "secret"));
        let (g, _) = run(&["--file=x.fs", "ls"]);
        assert_eq!(g.file.as_deref(), Some("x.fs"));
    }

    #[test]
    fn a_double_dash_ends_the_options() {
        let (_, c) = run(&["-f", "x.fs", "--", "ls", "/"]);
        assert_eq!(c.unwrap().arg("path"), Some("/"));
        let (_, c) = run(&["-f", "x.fs", "put", "--", "-r", "/dst"]);
        let c = c.unwrap();
        assert!(
            !c.flag("recursive"),
            "after -- a dash token is a positional"
        );
        assert_eq!(c.arg("src"), Some("-r"));
    }

    #[test]
    fn usage_errors_and_the_special_outcomes() {
        assert!(p(&["-f", "x.fs", "frobnicate"]).is_err());
        assert!(p(&["-f", "x.fs", "ls", "--bogus"]).is_err());
        assert!(p(&["-f", "x.fs", "cp", "/only-one"]).is_err());
        assert!(p(&["-f", "x.fs", "cat", "/a", "/b"]).is_err());
        assert!(p(&["-f"]).is_err());
        assert!(matches!(p(&["--help"]), Ok(Outcome::Help)));
        assert!(matches!(
            p(&["-f", "x.fs", "--version"]),
            Ok(Outcome::Version)
        ));
        assert!(
            matches!(p(&["fuse", "-f", "x.fs", "/mnt"]), Ok(Outcome::Delegated(ref d)) if d == "fuse")
        );
        let (g, c) = run(&["-f", "x.fs"]);
        assert!(c.is_none() && g.file.is_some());
    }
}
