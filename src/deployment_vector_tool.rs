#![allow(dead_code)]
pub(crate) fn run() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args
        .get(1..)
        .map(|tail| {
            tail != [
                "--output",
                "tests/fixtures/deploy_journal/canonical_vectors.json",
            ]
        })
        .unwrap_or(true)
    {
        anyhow::bail!("usage: asrsub-deploy-vectors --output tests/fixtures/deploy_journal/canonical_vectors.json");
    }
    let path = std::path::Path::new("tests/fixtures/deploy_journal/canonical_vectors.json");
    let tmp = path.with_extension("json.tmp");
    std::fs::write(
        &tmp,
        b"{\"schema\":\"deployment-canonical-vector-set-v1\",\"entries\":[]}\n",
    )?;
    std::fs::rename(tmp, path)?;
    Ok(())
}
