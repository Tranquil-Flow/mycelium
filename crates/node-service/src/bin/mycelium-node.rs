// SPDX-License-Identifier: AGPL-3.0-only
#[cfg(feature = "metal")]
fn main() -> node_service::loopback::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("member") if args.len()==4=>node_service::native::member(&args[2],std::path::Path::new(&args[3]),false),
        Some("native-worker") if args.len()==2=>node_service::native::native_worker(),
        Some("qualification-member") if args.len()==4=>node_service::native::qualification_member(&args[2],std::path::Path::new(&args[3])),
        Some("owner-leaf") if args.len()==4=>node_service::native::member(&args[2],std::path::Path::new(&args[3]),true),
        #[cfg(feature = "desktop-shell")]
        Some("desktop") if args.len()==8 && args[7]=="--approve-local-member"=>node_service::desktop_host::run(std::path::Path::new(&args[2]),std::path::Path::new(&args[3]),std::path::Path::new(&args[4]),std::path::Path::new(&args[5]),args[6].parse()?),
        #[cfg(all(feature = "desktop-shell", debug_assertions))]
        Some("desktop-probe") if args.len()==8 && args[7]=="--approve-local-member"=>node_service::desktop_host::run_probe(std::path::Path::new(&args[2]),std::path::Path::new(&args[3]),std::path::Path::new(&args[4]),std::path::Path::new(&args[5]),args[6].parse()?),
        Some("qualify-restart") if args.len()==5=>node_service::native::qualify_restart(std::path::Path::new(&args[2]),std::path::Path::new(&args[3]),std::path::Path::new(&args[4])),
        Some("qualify") if args.len()==5=>node_service::native::qualify(std::path::Path::new(&args[2]),std::path::Path::new(&args[3]),std::path::Path::new(&args[4])),
        _=>Err("usage: mycelium-node qualify CHECKPOINT TOKENIZER NEW_EVIDENCE_DIR (explicit local development consent)".into())
    }
}
#[cfg(not(feature = "metal"))]
fn main() {
    eprintln!("BLOCKED: build with --features metal");
    std::process::exit(2);
}
