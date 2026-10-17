// Home Energy desktop app: a small always-on-top widget plus the full dashboard,
// both reading from the collector on your home network. Lives in the tray / menu bar.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::Read;

use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, WindowEvent,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

/// Only installers published as releases of this project may be downloaded and run.
const RELEASE_PREFIX: &str = "https://github.com/copperOstrich/sigen-dashboard/releases/download/";

fn show(app: &AppHandle, label: &str) {
    if let Some(w) = app.get_webview_window(label) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn hide(app: &AppHandle, label: &str) {
    if let Some(w) = app.get_webview_window(label) {
        let _ = w.hide();
    }
}

/// Switch from the compact widget to the full dashboard window.
#[tauri::command]
fn open_dashboard(app: AppHandle) {
    show(&app, "dashboard");
    hide(&app, "widget");
}

/// Switch from the full dashboard window to the compact widget.
#[tauri::command]
fn show_widget(app: AppHandle) {
    show(&app, "widget");
    hide(&app, "dashboard");
}

#[tauri::command]
fn hide_widget(app: AppHandle) {
    hide(&app, "widget");
}

/// Download a release installer and start it, then quit so the installer can replace the app.
#[tauri::command]
async fn install_update(app: AppHandle, url: String, name: String) -> Result<(), String> {
    if !url.starts_with(RELEASE_PREFIX) {
        return Err("refusing to download an installer from outside this project's releases".into());
    }
    let file_name: String = name
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || ".-_".contains(c) { c } else { '_' })
        .collect();
    let path = std::env::temp_dir().join(file_name);

    let dl_path = path.clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
        let resp = ureq::get(&url)
            .set("User-Agent", "home-energy-updater")
            .call()
            .map_err(|e| format!("download failed: {e}"))?;
        let mut bytes = Vec::new();
        resp.into_reader()
            .take(500 * 1024 * 1024)
            .read_to_end(&mut bytes)
            .map_err(|e| format!("download failed: {e}"))?;
        std::fs::write(&dl_path, &bytes).map_err(|e| format!("couldn't save the installer: {e}"))
    })
    .await
    .map_err(|e| e.to_string())??;

    #[cfg(target_os = "windows")]
    {
        let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
        let started = if ext == "msi" {
            std::process::Command::new("msiexec").arg("/i").arg(&path).spawn()
        } else {
            std::process::Command::new(&path).spawn()
        };
        started.map_err(|e| format!("couldn't start the installer: {e}"))?;
        app.exit(0);
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("couldn't open the installer: {e}"))?;
        let _ = &app;
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let _ = (&app, &path);
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app, "dashboard");
        }))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .invoke_handler(tauri::generate_handler![open_dashboard, show_widget, hide_widget, install_update])
        .setup(|app| {
            let start_at_login = app.autolaunch().is_enabled().unwrap_or(false);

            let widget = MenuItem::with_id(app, "widget", "Switch to widget", true, None::<&str>)?;
            let dashboard = MenuItem::with_id(app, "dashboard", "Open dashboard", true, None::<&str>)?;
            let on_top = CheckMenuItem::with_id(app, "ontop", "Keep widget on top", true, true, None::<&str>)?;
            let login = CheckMenuItem::with_id(app, "login", "Start at login", true, start_at_login, None::<&str>)?;
            let updates = MenuItem::with_id(app, "updates", "Check for updates", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Home Energy", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[&widget, &dashboard, &sep, &on_top, &login, &updates, &sep, &quit],
            )?;

            let on_top_item = on_top.clone();
            let login_item = login.clone();

            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Home Energy")
                .menu(&menu)
                .menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "widget" => {
                        show(app, "widget");
                        hide(app, "dashboard");
                    }
                    "dashboard" => {
                        show(app, "dashboard");
                        hide(app, "widget");
                    }
                    "ontop" => {
                        let checked = on_top_item.is_checked().unwrap_or(true);
                        if let Some(w) = app.get_webview_window("widget") {
                            let _ = w.set_always_on_top(checked);
                        }
                    }
                    "login" => {
                        let auto = app.autolaunch();
                        let _ = if login_item.is_checked().unwrap_or(false) { auto.enable() } else { auto.disable() };
                    }
                    "updates" => {
                        show(app, "dashboard");
                        let _ = app.emit_to("dashboard", "check-updates", ());
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    // Left click flips between the widget and the dashboard
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        let app = tray.app_handle();
                        let widget_visible = app
                            .get_webview_window("widget")
                            .and_then(|w| w.is_visible().ok())
                            .unwrap_or(false);
                        if widget_visible {
                            show(app, "dashboard");
                            hide(app, "widget");
                        } else {
                            show(app, "widget");
                            hide(app, "dashboard");
                        }
                    }
                })
                .build(app)?;
            Ok(())
        })
        // Closing a window hides it; the app keeps running in the tray / menu bar.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Home Energy");
}
