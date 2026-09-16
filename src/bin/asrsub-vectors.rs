#[path = "../discord_state_codec.rs"]
mod discord_state_codec;
#[path = "../discord_state_schema.rs"]
mod discord_state_schema;
#[path = "../discord_text.rs"]
mod discord_text;
#[path = "../discord_types.rs"]
mod discord_types;
#[path = "../discord_unicode.rs"]
mod discord_unicode;
#[path = "../vector_tool.rs"]
mod vector_tool;

fn main() {
    vector_tool::run_core();
}
