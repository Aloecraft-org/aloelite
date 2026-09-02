//! The fixtures themselves, checked against the spec, so a scenario cannot
//! quietly invent an operation, a harness, or an error code that no
//! implementation owes. The same six checks as the Python runner, plus the
//! projection of this crate's error enum onto the spec's closed set.

use std::collections::BTreeSet;

use aloelite_conformance::harness::HARNESSES;
use aloelite_conformance::scenarios::{self, INSPECTIONS, MOUNT_API};
use aloelite_core::FsError;
use serde_norway::Value as Yaml;

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

macro_rules! conformance_test {
    ($(#[$m:meta])* fn $name:ident() $body:block) => {
        $(#[$m])*
        #[cfg_attr(not(all(target_arch = "wasm32", target_os = "unknown")), test)]
        #[cfg_attr(
            all(target_arch = "wasm32", target_os = "unknown"),
            wasm_bindgen_test::wasm_bindgen_test
        )]
        fn $name() $body
    };
}

fn spec() -> Yaml {
    serde_norway::from_str(MOUNT_API).expect("mount-api.yaml parses")
}

fn declared_ops() -> BTreeSet<String> {
    let spec = spec();
    let mut names = BTreeSet::new();
    for (_, entries) in spec["operations"].as_mapping().expect("operations") {
        if let Some(group) = entries.as_mapping() {
            for (op, _) in group {
                let op = op.as_str().expect("op names are strings");
                if op != "note" {
                    names.insert(op.to_owned());
                }
            }
        }
    }
    for op in spec["id_variants"]["applies_to"]
        .as_sequence()
        .expect("applies_to")
    {
        names.insert(format!("{}_by_id", op.as_str().unwrap()));
    }
    names
}

fn declared_errors() -> BTreeSet<String> {
    spec()["errors"]
        .as_mapping()
        .expect("errors")
        .keys()
        .map(|k| k.as_str().unwrap().to_owned())
        .collect()
}

conformance_test!(
    fn scenarios_only_use_declared_operations() {
        let mut declared = declared_ops();
        declared.extend(INSPECTIONS.iter().map(|s| (*s).to_owned()));
        for (file, sc) in scenarios::all() {
            for step in sc["steps"].as_sequence().unwrap() {
                let op = step["op"].as_str().unwrap();
                assert!(
                    declared.contains(op),
                    "{file} / {}: {op} is not in mount-api.yaml",
                    sc["scenario"].as_str().unwrap()
                );
            }
        }
    }
);

conformance_test!(
    /// A typo in a harness name would otherwise skip the scenario forever,
    /// which reads exactly like passing.
    fn scenarios_only_name_implemented_harnesses() {
        for (file, sc) in scenarios::all() {
            let harness = sc
                .get("harness")
                .and_then(Yaml::as_str)
                .unwrap_or("default");
            assert!(
                HARNESSES.contains(&harness),
                "{file}: unknown harness {harness:?}"
            );
        }
    }
);

conformance_test!(
    /// YAML 1.1 reads bare on/off/yes/no as booleans, YAML 1.2 as strings.
    /// A key like `on:` would parse DIFFERENTLY in Python and here.
    fn no_scenario_key_is_a_yaml_boolean() {
        fn walk(node: &Yaml, at: &str) {
            match node {
                Yaml::Mapping(m) => {
                    for (k, v) in m {
                        assert!(
                            k.is_string(),
                            "{at}: key {k:?} is not a string — quote it or rename it; bare on/off/yes/no/true/false are not portable across YAML versions"
                        );
                        walk(v, &format!("{at}.{}", k.as_str().unwrap()));
                    }
                }
                Yaml::Sequence(s) => {
                    for (i, item) in s.iter().enumerate() {
                        walk(item, &format!("{at}[{i}]"));
                    }
                }
                _ => {}
            }
        }
        for (name, text) in scenarios::FILES {
            walk(&scenarios::parse(name, text), name);
        }
    }
);

conformance_test!(
    fn scenarios_only_expect_declared_errors() {
        let closed = declared_errors();
        for (file, sc) in scenarios::all() {
            for step in sc["steps"].as_sequence().unwrap() {
                if let Some(code) = step.get("raises").and_then(Yaml::as_str) {
                    assert!(
                        closed.contains(code),
                        "{file}: {code} is not a declared error"
                    );
                }
            }
        }
    }
);

conformance_test!(
    fn scenario_names_are_unique() {
        let names: Vec<String> = scenarios::all()
            .iter()
            .map(|(_, sc)| sc["scenario"].as_str().unwrap().to_owned())
            .collect();
        let unique: BTreeSet<&String> = names.iter().collect();
        assert_eq!(
            names.len(),
            unique.len(),
            "scenario names are test ids; they must be unique"
        );
    }
);

conformance_test!(
    fn every_scenario_cites_requirements() {
        for (file, sc) in scenarios::all() {
            let cited = sc
                .get("requirements")
                .and_then(Yaml::as_sequence)
                .is_some_and(|r| !r.is_empty());
            assert!(
                cited,
                "{file} / {}: no requirement ids cited",
                sc["scenario"].as_str().unwrap()
            );
        }
    }
);

conformance_test!(
    /// The engine's closed error set IS the spec's, in both directions.
    fn error_codes_project_onto_the_spec_in_both_directions() {
        let ours: BTreeSet<String> = FsError::CODES.iter().map(|c| (*c).to_owned()).collect();
        assert_eq!(ours, declared_errors());
        assert_eq!(ours.len(), FsError::CODES.len(), "no duplicate codes");
    }
);
