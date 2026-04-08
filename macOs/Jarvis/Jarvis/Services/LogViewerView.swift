import SwiftUI
import Combine

struct LogViewerView: View {
    @State private var logContent: String = "Loading logs..."
    @State private var isAutoRefreshing = true
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".jarvis/jarvis.log")
    
    private let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Server Logs")
                    .font(.headline)
                
                Spacer()
                
                HStack(spacing: 12) {
                    Toggle(isOn: $isAutoRefreshing) {
                        HStack(spacing: 4) {
                            Image(systemName: isAutoRefreshing ? "arrow.clockwise.circle.fill" : "arrow.clockwise.circle")
                            Text("Auto-refresh")
                        }
                        .font(.caption)
                    }
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    
                    Button {
                        loadLogs()
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    
                    Button {
                        clearLogs()
                    } label: {
                        Label("Clear", systemImage: "trash")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    
                    Button {
                        NSWorkspace.shared.open(logURL)
                    } label: {
                        Label("Open in Editor", systemImage: "arrow.up.forward.square")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
            }
            .padding()
            .background(Color(nsColor: .controlBackgroundColor))
            
            Divider()
            
            // Log content
            ScrollViewReader { proxy in
                ScrollView {
                    Text(logContent)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(12)
                        .textSelection(.enabled)
                        .id("logContent")
                }
                .background(Color(nsColor: .textBackgroundColor))
                .onChange(of: logContent) { _, _ in
                    if isAutoRefreshing {
                        withAnimation {
                            proxy.scrollTo("logContent", anchor: .bottom)
                        }
                    }
                }
            }
        }
        .frame(minWidth: 600, minHeight: 400)
        .onAppear {
            loadLogs()
        }
        .onReceive(timer) { _ in
            if isAutoRefreshing {
                loadLogs()
            }
        }
    }
    
    private func loadLogs() {
        if let content = try? String(contentsOf: logURL, encoding: .utf8) {
            // Get last 10000 lines to avoid memory issues
            let lines = content.split(separator: "\n", omittingEmptySubsequences: false)
            logContent = lines.suffix(10000).joined(separator: "\n")
        } else {
            logContent = "No logs found at \(logURL.path(percentEncoded: false))\n\nThe log file will be created when the server starts."
        }
    }
    
    private func clearLogs() {
        try? "".write(to: logURL, atomically: true, encoding: .utf8)
        logContent = "Logs cleared."
    }
}

#Preview {
    LogViewerView()
}