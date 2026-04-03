import SwiftUI

struct ContentView: View {
    @EnvironmentObject var state: AppState
    @State private var selectedServer: String?
    @State private var showSettings = false
    @State private var showError = false
    @State private var errorMessage = ""
    @State private var showLogs = false
    @State private var logContent = "Loading logs..."
    @State private var isAutoRefreshing = true
    
    private var logURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".jarvis/jarvis.log")
    }

    var sortedNames: [String] { state.servers.keys.sorted() }

    var body: some View {
        NavigationSplitView {
            serverList
        } detail: {
            if let name = selectedServer, let server = state.servers[name] {
                ServerDetailView(name: name, server: server)
                    .id(name)
            } else {
                emptyDetail
            }
        }
        .toolbar {
            ToolbarItem(placement: .navigation) {
                statusBadge
            }
            ToolbarItemGroup(placement: .primaryAction) {
                Button {
                    showLogs = true
                } label: {
                    Label("View Logs", systemImage: "doc.text.magnifyingglass")
                }
                .help("View server logs")
                
                Button {
                    openConfigFile()
                } label: {
                    Label("Edit Config", systemImage: "doc.text")
                }
                .help("Open servers.json in text editor")
                
                if state.processManager.isStarting {
                    HStack(spacing: 8) {
                        ProgressView()
                            .scaleEffect(0.7)
                            .controlSize(.small)
                        Text("Starting...")
                            .font(.callout)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.orange.opacity(0.2))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                } else if state.processManager.isRunning {
                    Button {
                        state.stopServer()
                    } label: {
                        Label("Stop Server", systemImage: "stop.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    .help("Stop the MCP server")
                } else {
                    Button {
                        // Prevent multiple clicks
                        guard !state.processManager.isStarting else { return }
                        
                        // Validate UV path before starting
                        if !FileManager.default.isExecutableFile(atPath: state.uvPath) {
                            errorMessage = "UV executable not found at:\n\(state.uvPath)\n\nPlease check Settings and update the UV path."
                            showError = true
                        } else {
                            state.startServer()
                            // Check if there was an error starting
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                                if let error = state.processManager.lastError {
                                    errorMessage = error
                                    showError = true
                                }
                            }
                        }
                    } label: {
                        Label("Start Server", systemImage: "play.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.green)
                    .help("Start the MCP server")
                }

                Button {
                    showSettings = true
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
                .help("Open settings")
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView()
                .environmentObject(state)
        }
        .sheet(isPresented: $showLogs) {
            NavigationStack {
                VStack(spacing: 0) {
                    // Log content
                    ScrollView {
                        Text(logContent)
                            .font(.system(.caption, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .textSelection(.enabled)
                    }
                    .background(Color(nsColor: .textBackgroundColor))
                }
                .navigationTitle("Server Logs")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") {
                            showLogs = false
                        }
                    }
                    ToolbarItem(placement: .automatic) {
                        Toggle(isOn: $isAutoRefreshing) {
                            Label("Auto-refresh", systemImage: "arrow.clockwise")
                        }
                        .toggleStyle(.switch)
                    }
                    ToolbarItem(placement: .automatic) {
                        Button("Refresh") {
                            loadLogs()
                        }
                    }
                    ToolbarItem(placement: .automatic) {
                        Button("Clear") {
                            clearLogs()
                        }
                    }
                    ToolbarItem(placement: .automatic) {
                        Button("Open in Editor") {
                            NSWorkspace.shared.open(logURL)
                        }
                    }
                }
                .onAppear {
                    loadLogs()
                    startAutoRefresh()
                }
                .onDisappear {
                    isAutoRefreshing = false
                }
                .frame(minWidth: 700, minHeight: 500)
            }
        }
        .alert("Error Starting Server", isPresented: $showError) {
            Button("Open Settings") {
                showError = false
                showSettings = true
            }
            Button("OK", role: .cancel) {
                showError = false
            }
        } message: {
            Text(errorMessage)
        }
        .frame(minWidth: 680, minHeight: 460)
    }

    // MARK: - Subviews

    private var serverList: some View {
        List(sortedNames, id: \.self, selection: $selectedServer) { name in
            ServerRowView(name: name, server: state.servers[name]!)
        }
        .navigationTitle("MCP Servers")
        .navigationSplitViewColumnWidth(min: 220, ideal: 260)
        .overlay {
            if state.servers.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "hexagon")
                        .font(.system(size: 48))
                        .foregroundStyle(.tertiary)
                    Text("No servers configured")
                        .font(.headline)
                        .foregroundStyle(.secondary)
                    Text("Add servers to ~/.jarvis/servers.json")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button("Open Config File") {
                        openConfigFile()
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
        }
    }
    
    private func openConfigFile() {
        let configURL = state.configURL
        
        // Create file if it doesn't exist
        if !FileManager.default.fileExists(atPath: configURL.path) {
            state.saveConfig()
        }
        
        // Open in default text editor
        NSWorkspace.shared.open(configURL)
    }
    
    // MARK: - Log Viewer
    
    private func loadLogs() {
        if let content = try? String(contentsOf: logURL, encoding: .utf8) {
            // Get last 10000 lines to avoid memory issues
            let lines = content.split(separator: "\n", omittingEmptySubsequences: false)
            let recentLines = lines.suffix(10000)
            logContent = recentLines.joined(separator: "\n")
        } else {
            logContent = "No logs found at \(logURL.path)\n\nThe log file will be created when the server starts."
        }
    }
    
    private func clearLogs() {
        try? "".write(to: logURL, atomically: true, encoding: .utf8)
        logContent = "Logs cleared."
    }
    
    private func startAutoRefresh() {
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { timer in
            if !showLogs || !isAutoRefreshing {
                timer.invalidate()
                return
            }
            loadLogs()
        }
    }

    private var emptyDetail: some View {
        VStack(spacing: 8) {
            Image(systemName: "hexagon")
                .font(.system(size: 48))
                .foregroundStyle(.tertiary)
            Text("Select a server")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var statusBadge: some View {
        HStack(spacing: 8) {
            if state.processManager.isStarting {
                ProgressView()
                    .scaleEffect(0.6)
                    .controlSize(.small)
            } else {
                Circle()
                    .fill(state.processManager.isRunning ? Color.green : Color.secondary.opacity(0.5))
                    .frame(width: 10, height: 10)
            }
            
            VStack(alignment: .leading, spacing: 2) {
                Text(state.processManager.isStarting 
                     ? "Server Starting..." 
                     : (state.processManager.isRunning ? "Server Running" : "Server Stopped"))
                    .font(.callout)
                    .fontWeight(.medium)
                if state.processManager.isRunning {
                    Text(state.processManager.endpoint)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else if state.processManager.isStarting {
                    Text("Waiting for server to respond...")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
    }
}

// MARK: - Server row

struct ServerRowView: View {
    let name: String
    let server: MCPServer
    @EnvironmentObject var state: AppState

    var isEnabled: Bool { server.enabled ?? true }

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(name)
                    .fontWeight(.medium)
                    .foregroundStyle(isEnabled ? .primary : .secondary)
                Text(server.displayType)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            if server.isOAuth {
                Image(systemName: "key.fill")
                    .foregroundStyle(.orange)
                    .font(.caption)
            }
            Toggle("", isOn: Binding(
                get: { server.enabled ?? true },
                set: { newValue in
                    state.servers[name]?.enabled = newValue
                    state.saveConfig()
                }
            ))
            .labelsHidden()
        }
        .padding(.vertical, 2)
    }
}
