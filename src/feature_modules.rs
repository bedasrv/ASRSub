//! Feature-root declarations.  Keeping these leaves explicit lets focused
//! binaries reuse the pure contracts without importing the daemon.

#[path = "discord_config.rs"]
pub(crate) mod discord_config;
#[path = "discord_renderer.rs"]
pub(crate) mod discord_renderer;
#[path = "discord_state.rs"]
pub(crate) mod discord_state;
#[path = "discord_state_clock.rs"]
pub(crate) mod discord_state_clock;
#[path = "discord_state_codec.rs"]
pub(crate) mod discord_state_codec;
#[path = "discord_state_engine.rs"]
pub(crate) mod discord_state_engine;
#[path = "discord_state_lane.rs"]
pub(crate) mod discord_state_lane;
#[path = "discord_state_local.rs"]
pub(crate) mod discord_state_local;
#[path = "discord_state_outbox.rs"]
pub(crate) mod discord_state_outbox;
#[path = "discord_state_schema.rs"]
pub(crate) mod discord_state_schema;
#[path = "discord_text.rs"]
pub(crate) mod discord_text;
#[path = "discord_transport.rs"]
pub(crate) mod discord_transport;
#[path = "discord_types.rs"]
pub(crate) mod discord_types;
#[path = "discord_unicode.rs"]
pub(crate) mod discord_unicode;
#[path = "pipeline_commit.rs"]
pub(crate) mod pipeline_commit;
#[path = "process.rs"]
pub(crate) mod process;
