#[path = "../state_control.rs"]
mod state_control;
fn main() {
    let _ = state_control::run(&std::env::args().collect::<Vec<_>>());
}
