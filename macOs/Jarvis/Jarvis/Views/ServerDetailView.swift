import SwiftUI

struct ServerDetailView: View {
    let name: String
    let server: MCPServer
    let onBack: (() -> Void)?
    @EnvironmentObject var state: AppState
    @State private var newEnvKey = ""
    @State private var newEnvValue = ""
    @State private var stagedServer: MCPServer
    @State private var hasChanges = false

    // Stable-identity wrapper for args to avoid index-based ForEach issues
    struct ArgItem: Identifiable {
        let id: UUID
        var value: String
    }
    @State private var argItems: [ArgItem] = []

    init(name: String, server: MCPServer, onBack: (() -> Void)? = nil) {
        self.name = name
        self.server = server
        self.onBack = onBack
        _stagedServer = State(initialValue: server)
        _argItems = State(initialValue: (server.args ?? []).map { ArgItem(id: UUID(), value: $0) })
    }

    private func binding<T: Equatable>(for keyPath: WritableKeyPath<MCPServer, T>) -> Binding<T> {
        Binding(
            get: { stagedServer[keyPath: keyPath] },
            set: { newValue in
                stagedServer[keyPath: keyPath] = newValue
                hasChanges = true
            }
        )
    }

    private func optionalStringBinding(for keyPath: WritableKeyPath<MCPServer, String?>) -> Binding<String> {
        Binding(
            get: { stagedServer[keyPath: keyPath] ?? "" },
            set: { newValue in
                stagedServer[keyPath: keyPath] = newValue.isEmpty ? nil : newValue
                hasChanges = true
            }
        )
    }

    private func syncArgsToServer() {
        if argItems.isEmpty {
            stagedServer.args = nil
        } else {
            stagedServer.args = argItems.map { $0.value }
        }
    }

    private func applyChanges() {
        state.servers[name] = stagedServer
        state.saveConfig()
        hasChanges = false
        // Restart server if running to apply changes
        if state.processManager.isRunning {
            state.restartServer()
        }
    }

    var body: some View {
        Form {
            // Connection
            Section("Connection") {
                if server.isHTTP {
                    TextField("URL", text: optionalStringBinding(for: \.url))
                        .textFieldStyle(.roundedBorder)
                    LabeledContent("Transport", value: server.transport ?? "http")
                } else {
                    TextField("Command", text: optionalStringBinding(for: \.command))
                        .textFieldStyle(.roundedBorder)

                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("Args")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button {
                                argItems.append(ArgItem(id: UUID(), value: ""))
                                syncArgsToServer()
                                hasChanges = true
                            } label: {
                                Image(systemName: "plus.circle")
                                    .foregroundStyle(.green)
                            }
                            .buttonStyle(.borderless)
                        }

                        if !argItems.isEmpty {
                            ForEach(argItems) { item in
                                HStack {
                                    TextField("Arg", text: Binding(
                                        get: { item.value },
                                        set: { newValue in
                                            if let index = argItems.firstIndex(where: { $0.id == item.id }) {
                                                argItems[index].value = newValue
                                                syncArgsToServer()
                                                hasChanges = true
                                            }
                                        }
                                    ))
                                    .textFieldStyle(.roundedBorder)

                                    Button {
                                        argItems.removeAll { $0.id == item.id }
                                        syncArgsToServer()
                                        hasChanges = true
                                    } label: {
                                        Image(systemName: "minus.circle")
                                            .foregroundStyle(.red)
                                    }
                                    .buttonStyle(.borderless)
                                }
                            }
                        } else {
                            Text("No arguments")
                                .foregroundStyle(.tertiary)
                                .font(.caption)
                        }
                    }

                    LabeledContent("Transport", value: "stdio")
                }
            }

            // Environment
            Section {
                if let env = stagedServer.env, !env.isEmpty {
                    ForEach(env.keys.sorted(), id: \.self) { key in
                        HStack {
                            Text(key)
                                .frame(minWidth: 100, alignment: .leading)
                            TextField("Value", text: Binding(
                                get: { stagedServer.env?[key] ?? "" },
                                set: { newValue in
                                    stagedServer.env?[key] = newValue
                                    hasChanges = true
                                }
                            ))
                            .textFieldStyle(.roundedBorder)
                            Button {
                                stagedServer.env?.removeValue(forKey: key)
                                if stagedServer.env?.isEmpty == true {
                                    stagedServer.env = nil
                                }
                                hasChanges = true
                            } label: {
                                Image(systemName: "minus.circle")
                                    .foregroundStyle(.red)
                            }
                            .buttonStyle(.borderless)
                        }
                    }
                }
                HStack {
                    TextField("Key", text: $newEnvKey)
                        .textFieldStyle(.roundedBorder)
                        .frame(minWidth: 100)
                    TextField("Value", text: $newEnvValue)
                        .textFieldStyle(.roundedBorder)
                    Button {
                        guard !newEnvKey.isEmpty else { return }
                        if stagedServer.env == nil {
                            stagedServer.env = [:]
                        }
                        stagedServer.env?[newEnvKey] = newEnvValue
                        hasChanges = true
                        newEnvKey = ""
                        newEnvValue = ""
                    } label: {
                        Image(systemName: "plus.circle")
                            .foregroundStyle(.green)
                    }
                    .buttonStyle(.borderless)
                    .disabled(newEnvKey.isEmpty)
                }
            } header: {
                Text("Environment")
            }

            // Tools
            Section {
                if let tools = state.discoveredTools[name], !tools.isEmpty {
                    ForEach(tools) { tool in
                        ToolRowView(
                            serverName: name,
                            tool: tool,
                            isDisabled: state.isToolDisabled(server: name, tool: tool.name)
                        )
                    }
                } else if state.isDiscoveringTools {
                    HStack {
                        ProgressView()
                            .controlSize(.small)
                        Text("Discovering tools...")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("No tools discovered yet")
                        .foregroundStyle(.secondary)
                }

                if state.servers[name]?.requiresRestart == true {
                    HStack {
                        Spacer()
                        Button("Apply Tool Changes & Restart") {
                            state.servers[name]?.requiresRestart = false
                            applyChanges()
                        }
                        .buttonStyle(.borderedProminent)
                    }
                }
            } header: {
                HStack {
                    Text("Tools")
                    Spacer()
                    if let tools = state.discoveredTools[name] {
                        let enabled = tools.filter { !state.isToolDisabled(server: name, tool: $0.name) }.count
                        Text("\(enabled)/\(tools.count)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Button {
                        state.discoverTools()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .disabled(state.isDiscoveringTools || !(server.enabled ?? true))
                }
            }

            // Status
            Section("Status") {
                LabeledContent("Enabled") {
                    Toggle("", isOn: Binding(
                        get: { stagedServer.enabled ?? true },
                        set: { newValue in
                            stagedServer.enabled = newValue
                            hasChanges = true
                        }
                    ))
                    .labelsHidden()
                }

                if hasChanges {
                    HStack {
                        Spacer()
                        Button("Discard Changes") {
                            // Reset to the current authoritative server state
                            let currentServer = state.servers[name] ?? server
                            stagedServer = currentServer
                            argItems = (currentServer.args ?? []).map { ArgItem(id: UUID(), value: $0) }
                            hasChanges = false
                        }
                        .foregroundStyle(.secondary)

                        Button("Apply & Restart") {
                            applyChanges()
                        }
                        .buttonStyle(.borderedProminent)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle(name)
        .navigationSubtitle(server.isOAuth ? "OAuth" : (server.isHTTP ? "HTTP" : "stdio"))
        .toolbar {
            if let onBack {
                ToolbarItem(placement: .navigation) {
                    Button {
                        onBack()
                    } label: {
                        Label("All Servers", systemImage: "chevron.left")
                    }
                    .help("Back to overview")
                }
            }
        }
        .onChange(of: server) { newServer in
            // Sync stagedServer when the authoritative server changes externally
            // (e.g., from file watcher, preset switch, or tool toggle)
            if !hasChanges {
                stagedServer = newServer
                argItems = (newServer.args ?? []).map { ArgItem(id: UUID(), value: $0) }
            }
        }
    }
}

struct ToolRowView: View {
    let serverName: String
    let tool: DiscoveredTool
    let isDisabled: Bool
    @EnvironmentObject var state: AppState

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(tool.name)
                    .fontWeight(.medium)
                    .foregroundStyle(isDisabled ? .secondary : .primary)
                if !tool.description.isEmpty {
                    Text(tool.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            if state.servers[serverName]?.requiresRestart == true {
                Text("Restart required")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            Toggle("", isOn: Binding(
                get: { !isDisabled },
                set: { _ in
                    state.toggleTool(server: serverName, tool: tool.name)
                }
            ))
            .labelsHidden()
        }
    }
}