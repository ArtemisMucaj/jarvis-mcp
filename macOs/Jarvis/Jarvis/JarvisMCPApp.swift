import SwiftUI

/// Ensures the server process is killed when the app terminates (Cmd+Q, force quit, etc.)
class AppDelegate: NSObject, NSApplicationDelegate {
    var state: AppState?

    func applicationWillTerminate(_ notification: Notification) {
        state?.stopServer()
    }
}

@main
struct JarvisMCPApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var state = AppState()

    @ViewBuilder
    private func menuBarImage(opacity: Double) -> some View {
        if let nsImage = NSImage(named: "MenuBarIcon") {
            let templateImage = nsImage.copy() as! NSImage
            let _ = { templateImage.isTemplate = true }()
            Image(nsImage: templateImage)
                .opacity(opacity)
        }
    }

    var body: some Scene {
        // Main window - opens by default
        WindowGroup {
            ContentView()
                .environmentObject(state)
                .onAppear { appDelegate.state = state }
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
                    menuBarImage(opacity: 1.0)
                }
            } else {
                menuBarImage(opacity: state.processManager.isRunning ? 1.0 : 0.5)
            }
        }
        .menuBarExtraStyle(.menu)
    }
}
