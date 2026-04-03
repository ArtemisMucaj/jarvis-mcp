import SwiftUI

/// Ensures the server process is killed when the app terminates (Cmd+Q, force quit, etc.)
class AppDelegate: NSObject, NSApplicationDelegate {
    var state: AppState?

    func applicationWillTerminate(_ notification: Notification) {
        state?.stopServer()
    }
}

/// Pre-built image for the menu bar — loaded once at startup.
private let menuBarNSImage: NSImage? = {
    if let img = NSImage(named: "MenuBarIcon") {
        return img
    }
    return nil
}()

@main
struct JarvisMCPApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var state = AppState()

    var body: some Scene {
        // Main window - opens by default
        WindowGroup {
            ContentView()
                .environmentObject(state)
                .onAppear {
                    appDelegate.state = state
                    state.startServer()
                }
        }
        .defaultSize(width: 780, height: 520)
        
        // Menu bar extra for quick access
        MenuBarExtra {
            MenuBarView()
                .environmentObject(state)
        } label: {
            if state.processManager.isStarting {
                HStack(spacing: 4) {
                    ProgressView()
                        .scaleEffect(0.6)
                        .controlSize(.small)
                    menuBarIcon(dimmed: false)
                }
            } else {
                menuBarIcon(dimmed: !state.processManager.isRunning)
            }
        }
        .menuBarExtraStyle(.menu)
    }

    @ViewBuilder
    private func menuBarIcon(dimmed: Bool) -> some View {
        if let img = menuBarNSImage {
            Image(nsImage: img)
                .resizable()
                .frame(width: 16, height: 16)
                .opacity(dimmed ? 0.5 : 1.0)
        } else {
            // Fallback if asset is missing
            Image(systemName: dimmed ? "j.circle" : "j.circle.fill")
        }
    }
}
