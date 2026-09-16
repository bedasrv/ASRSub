#[path = "../secret_probe.rs"]
mod secret_probe;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 || args[1] != "--expected-stdin" || args[2] != "--target" {
        std::process::exit(2);
    }
    let target = match args[3].as_str() {
        "/run/secrets/discord_webhook" | "/run/secrets/control_api_key" => args[3].clone(),
        _ => std::process::exit(2),
    };
    let mut input = Vec::new();
    if std::io::Read::read_to_end(&mut std::io::stdin(), &mut input).is_err() {
        std::process::exit(1);
    }
    match secret_probe::compare(std::path::Path::new(&target), &input) {
        Ok(true) => println!("MATCH"),
        Ok(false) => println!("MISMATCH"),
        Err(_) => std::process::exit(1),
    }
}
