//! `chatwalaau-computer-use` -- the ChatWalaau Computer Use desktop provider
//! (CAP-012, CTR-0236 `chatwalaau.computer-use/1`, PRP-0191, UDR-0173).
//!
//! A stdio MCP server started by the ChatWalaau backend on the first desktop call
//! (CTR-0237). stdout carries the MCP protocol ONLY; diagnostics go to stderr, which the
//! backend forwards to its log. The process ends when stdin closes (its parent is gone).
//!
//! It moves pixels and nothing else: it listens on no socket, runs no code, keeps the last
//! four captures in memory only and never logs an argument (UDR-0172 D9).

mod changes;
mod desktop;
#[cfg(feature = "fake-desktop")]
mod fake;
mod imaging;
mod platform;
mod protocol;
mod server;

use rmcp::ServiceExt;

fn usage() -> ! {
    eprintln!("usage: chatwalaau-computer-use [--version] [--list-tools]");
    std::process::exit(2);
}

fn main() {
    let mut fake_mode: Option<String> = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--version" => {
                println!("{} {}", protocol::PROVIDER_NAME, protocol::PROVIDER_VERSION);
                return;
            }
            "--list-tools" => {
                println!("{}", server::DesktopServer::tool_names().join("\n"));
                return;
            }
            "--fake" if cfg!(feature = "fake-desktop") => fake_mode = Some(args.next().unwrap_or_default()),
            _ => usage(),
        }
    }

    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(false)
        .with_target(false)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("CHATWALAAU_COMPUTER_USE_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn"))
                // rmcp's debug level prints whole requests -- text to type included, which can
                // be a secret value. It never goes below warn (UDR-0172 D9).
                .add_directive("rmcp=warn".parse().expect("static directive")),
        )
        .init();

    let desktop = match desktop::DesktopThread::spawn(move || backend(fake_mode)) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("desktop thread could not start: {e}");
            std::process::exit(1);
        }
    };
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("runtime could not start: {e}");
            std::process::exit(1);
        }
    };
    let result = runtime.block_on(async move {
        let service = server::DesktopServer::new(desktop).serve(rmcp::transport::stdio()).await?;
        service.waiting().await?;
        Ok::<(), Box<dyn std::error::Error>>(())
    });
    if let Err(e) = result {
        tracing::warn!("server ended: {e}");
    }
}

#[cfg(feature = "fake-desktop")]
fn backend(fake_mode: Option<String>) -> Result<Box<dyn desktop::Desktop>, String> {
    if let Some(mode) = fake_mode {
        let mode = fake::Mode::parse(&mode).ok_or_else(|| format!("unknown fake mode {mode:?}"))?;
        return Ok(Box::new(fake::FakeDesktop::new(mode)));
    }
    platform::real_desktop()
}

#[cfg(not(feature = "fake-desktop"))]
fn backend(_fake_mode: Option<String>) -> Result<Box<dyn desktop::Desktop>, String> {
    platform::real_desktop()
}
