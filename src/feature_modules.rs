//! Feature-root declarations.  Keeping these leaves explicit lets focused
//! binaries reuse the pure contracts without importing the daemon.

#[path = "discord_state_codec.rs"]
pub(crate) mod discord_state_codec;
#[path = "discord_state_schema.rs"]
pub(crate) mod discord_state_schema;
#[path = "discord_text.rs"]
pub(crate) mod discord_text;
#[path = "discord_types.rs"]
pub(crate) mod discord_types;
#[path = "discord_unicode.rs"]
pub(crate) mod discord_unicode;
#[path = "pipeline_commit.rs"]
pub(crate) mod pipeline_commit;
#[path = "process.rs"]
pub(crate) mod process;
