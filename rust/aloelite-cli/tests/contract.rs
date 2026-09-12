//! The verb and global tables against `aloelite/config/cli.yaml`, in both
//! directions — the Rust twin of `tests/test_cli_contract.py`. A verb, a
//! positional, a flag or a global cannot exist here and not in the
//! contract, or in the contract and not here.

use std::collections::{BTreeMap, BTreeSet};

use aloelite_cli::args::{DELEGATIONS, GLOBALS, Scope, VERBS};
use serde_norway::Value as Yaml;

const CONTRACT: &str = include_str!("../../../aloelite/config/cli.yaml");

fn contract() -> Yaml {
    serde_norway::from_str(CONTRACT).expect("cli.yaml parses")
}

fn strings(v: &Yaml) -> Vec<String> {
    v.as_sequence()
        .map(|s| s.iter().map(|x| x.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

/// `[(name, optional)]` from a contract `args` list.
fn declared_args(spec: &Yaml) -> Vec<(String, bool)> {
    spec["args"]
        .as_sequence()
        .map(|s| {
            s.iter()
                .map(|a| {
                    (
                        a["name"].as_str().unwrap().to_owned(),
                        a["optional"].as_bool().unwrap_or(false),
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}

fn declared_flags(spec: &Yaml) -> BTreeMap<String, Vec<String>> {
    spec["flags"]
        .as_mapping()
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.as_str().unwrap().to_owned(), strings(v)))
                .collect()
        })
        .unwrap_or_default()
}

#[test]
fn verbs_match_in_both_directions() {
    let c = contract();
    let declared: BTreeSet<String> = c["verbs"]
        .as_mapping()
        .unwrap()
        .keys()
        .map(|k| k.as_str().unwrap().to_owned())
        .collect();
    let table: BTreeSet<String> = VERBS.iter().map(|v| v.name.to_owned()).collect();
    assert_eq!(
        declared, table,
        "verbs differ between cli.yaml and args::VERBS"
    );
}

#[test]
fn each_verb_has_the_declared_shape_and_scope() {
    let c = contract();
    for verb in VERBS {
        let spec = &c["verbs"][verb.name];
        let scope = match verb.scope {
            Scope::Mount => "mount",
            Scope::File => "file",
        };
        assert_eq!(
            spec["scope"].as_str().unwrap(),
            scope,
            "{}: scope",
            verb.name
        );
        let ours: Vec<(String, bool)> = verb
            .args
            .iter()
            .map(|p| (p.name.to_owned(), p.optional))
            .collect();
        assert_eq!(ours, declared_args(spec), "{}: positionals", verb.name);
        let flags: BTreeMap<String, Vec<String>> = verb
            .flags
            .iter()
            .map(|f| {
                (
                    f.name.to_owned(),
                    f.opts.iter().map(|s| (*s).to_owned()).collect(),
                )
            })
            .collect();
        assert_eq!(flags, declared_flags(spec), "{}: flags", verb.name);
        let subs: BTreeMap<String, Vec<(String, bool)>> = verb
            .sub
            .iter()
            .map(|s| {
                (
                    s.name.to_owned(),
                    s.args
                        .iter()
                        .map(|p| (p.name.to_owned(), p.optional))
                        .collect(),
                )
            })
            .collect();
        let declared_subs: BTreeMap<String, Vec<(String, bool)>> = spec["sub"]
            .as_mapping()
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.as_str().unwrap().to_owned(), declared_args(v)))
                    .collect()
            })
            .unwrap_or_default();
        assert_eq!(subs, declared_subs, "{}: sub-verbs", verb.name);
    }
}

#[test]
fn globals_match() {
    let c = contract();
    let declared: BTreeMap<String, (Vec<String>, bool)> = c["globals"]
        .as_mapping()
        .unwrap()
        .iter()
        .map(|(k, v)| {
            (
                k.as_str().unwrap().to_owned(),
                (
                    strings(&v["flags"]),
                    v["optional_value"].as_bool().unwrap_or(false),
                ),
            )
        })
        .collect();
    let ours: BTreeMap<String, (Vec<String>, bool)> = GLOBALS
        .iter()
        .map(|g| {
            (
                g.name.to_owned(),
                (
                    g.opts.iter().map(|s| (*s).to_owned()).collect(),
                    g.optional_value,
                ),
            )
        })
        .collect();
    assert_eq!(ours, declared);
    // a global with no value in the contract takes none here either
    for g in GLOBALS {
        let has_value = c["globals"][g.name].get("value").is_some();
        assert_eq!(g.value.is_some(), has_value, "{}: value", g.name);
    }
}

#[test]
fn delegations_match() {
    let c = contract();
    assert_eq!(strings(&c["delegations"]["names"]), DELEGATIONS);
}
