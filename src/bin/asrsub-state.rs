#[path = "../deployment_commands.rs"]
mod deployment_commands;
#[path = "../deployment_journal_codec.rs"]
mod deployment_journal_codec;
#[path = "../deployment_journal_schema.rs"]
mod deployment_journal_schema;
#[path = "../deployment_journal_store.rs"]
mod deployment_journal_store;
#[path = "../discord_fs.rs"]
mod discord_fs;
#[path = "../discord_fs_linux.rs"]
mod discord_fs_linux;
#[path = "../discord_state.rs"]
mod discord_state;
#[path = "../discord_state_clock.rs"]
mod discord_state_clock;
#[path = "../discord_state_codec.rs"]
mod discord_state_codec;
#[path = "../discord_state_lane.rs"]
mod discord_state_lane;
#[path = "../discord_state_local.rs"]
mod discord_state_local;
#[path = "../discord_state_schema.rs"]
mod discord_state_schema;
#[path = "../discord_text.rs"]
mod discord_text;
#[path = "../discord_types.rs"]
mod discord_types;
#[path = "../discord_unicode.rs"]
mod discord_unicode;
#[path = "../egress.rs"]
mod egress;
#[path = "../state_control.rs"]
mod state_control;
fn main() {
    if let Err(error) = state_control::run(&std::env::args().collect::<Vec<_>>()) {
        eprintln!("state-control-error:{error}");
        std::process::exit(1);
    }
}
