// Home Energy desktop app: a small always-on-top widget plus the full dashboard,
// both reading from the collector on your home network. Lives in the tray / menu bar.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WindowEvent,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

fn show(app: &AppHandle, label: &str) {
    if let Some(w) = app.get_webview_window(label) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

#[tauri::command]
fn open_dashboard(app: AppHandle) {
    show(&app, "dashboard");
}

#[tauri::command]
fn hide_widget(app: AppHandle) {
    if let Some(w) = app.get_webview_window("widget") {
        let _ = w.hide();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app, "dashboard");
        }))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .invoke_handler(tauri::generate_handler![open_dashboard, hide_widget])
        .setup(|app| {
            let start_at_login = app.autolaunch().is_enabled().unwrap_or(false);

            let widget = MenuItem::with_id(app, "widget", "Show widget", true, None::<&str>)?;
            let dashboard = MenuItem::with_id(app, "dashboard", "Open dashboard", true, None::<&str>)?;
            let on_top = CheckMenuItem::with_id(app, "ontop", "Keep widget on top", true, true, None::<&str>)?;
            let login = CheckMenuItem::with_id(app, "login", "Start at login", true, start_at_login, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Home Energy", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&widget, &dashboard, &sep, &on_top, &login, &sep, &quit])?;

            let on_top_item = on_top.clone();
            let login_item = login.clone();

            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Home Energy")
                .menu(&menu)
                .menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "widget" => show(app, "widget"),
                    "dashboard" => show(app, "dashboard"),
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
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show(tray.app_handle(), "widget");
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
