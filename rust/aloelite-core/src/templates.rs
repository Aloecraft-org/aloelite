//! The sixty shared SQL templates, as compile-time constants.
//!
//! Generated from `aloelite/config/sql-templates.yaml` by `build.rs`; see
//! that file for why. A template is one parameterized statement with named
//! `:binds`; the host owns everything between templates — transactions,
//! branching, the resolve fold, the copy/pack walk. Nothing here contains
//! control flow, and nothing here is edited by hand.

include!(concat!(env!("OUT_DIR"), "/templates.rs"));

/// `aloelite/sql/schema.sql`, verbatim: the tables, guard triggers, indexes
/// and views every implementation creates identically.
pub const SCHEMA_SQL: &str = include_str!("../../../aloelite/sql/schema.sql");
