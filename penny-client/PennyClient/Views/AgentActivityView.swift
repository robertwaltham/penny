import SwiftUI
import PennyServices

struct AgentActivityCard: View {
    let runs: [AgentProgressRunItem]
    let isTyping: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Label("Penny’s activity", systemImage: "list.bullet")
                    .font(.subheadline.weight(.semibold))
                Spacer(minLength: 8)
                if isTyping || runs.contains(where: \.isRunning) {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("Penny is working")
                }
            }

            if runs.isEmpty {
                Text("Penny is thinking…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            ForEach(runs) { run in
                AgentActivityRunView(run: run)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
    }
}

struct AgentActivityView: View {
    let client: PennyService

    var body: some View {
        NavigationStack {
            List {
                Section("Connection") {
                    LabeledContent("Status", value: client.statusText)
                    if let error = client.lastError {
                        Text(error)
                            .font(.subheadline)
                            .foregroundStyle(.red)
                        Button("Reconnect", systemImage: "arrow.clockwise") {
                            client.reconnect()
                        }
                    }
                }

                if !client.foregroundActivityRuns.isEmpty {
                    Section("Latest message") {
                        ForEach(client.foregroundActivityRuns) { run in
                            AgentActivityRunView(run: run)
                        }
                    }
                }

                Section("Background work") {
                    if client.backgroundProgressRuns.isEmpty {
                        Text("Nothing is running")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(client.backgroundProgressRuns) { run in
                            AgentActivityRunView(run: run, showsAgent: true)
                        }
                    }
                }
            }
            .navigationTitle("Agent activity")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct AgentActivityRunView: View {
    let run: AgentProgressRunItem
    var showsAgent = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if showsAgent {
                Text(run.agent.capitalized)
                    .font(.headline)
            }
            Text(run.activityStatus)
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
            if run.steps.isEmpty {
                Text(run.isRunning ? "Waiting for the first step…" : "No step details were received.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            ForEach(run.steps) { step in
                AgentActivityStepView(step: step)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct AgentActivityStepView: View {
    let step: AgentProgressStepItem
    @State private var isThinkingExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(step.activityTitle)
                .font(.subheadline.weight(.semibold))

            if let thinking = step.thinking, !thinking.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    DisclosureGroup(isExpanded: $isThinkingExpanded) {
                        Text(verbatim: thinking)
                            .font(.subheadline)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.top, 4)
                    } label: {
                        Label("Thinking", systemImage: "text.bubble")
                            .font(.subheadline)
                    }
                    if !isThinkingExpanded {
                        Text(verbatim: thinking)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(3)
                            .accessibilityHidden(true)
                    }
                }
            }

            ForEach(step.tools) { tool in
                AgentActivityToolView(tool: tool)
            }
        }
        .padding(.leading, 12)
        .padding(.vertical, 4)
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 1)
                .fill(Color.secondary.opacity(0.2))
                .frame(width: 2)
                .accessibilityHidden(true)
        }
    }
}

private struct AgentActivityToolView: View {
    let tool: AgentProgressToolItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(tool.activityLabel, systemImage: "wrench")
                .font(.subheadline)
            Text(tool.name)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

extension AgentProgressRunItem {
    var activityStatus: String {
        switch state {
        case .running: "Working"
        case .interrupted: "Connection interrupted"
        case .finished(.completed): "Completed"
        case .finished(.maxSteps): "Step limit reached"
        case .finished(.error): "Run failed"
        case .finished(nil): "Finished"
        }
    }
}

extension AgentProgressStepItem {
    var activityTitle: String {
        maxSteps.map { "Step \(number) of \($0)" } ?? "Step \(number)"
    }
}

extension AgentProgressToolItem {
    var activityLabel: String {
        switch name {
        case "browse":
            if case .array(let queries)? = arguments["queries"],
               let query = queries.compactMap({ value in
                   if case .string(let string) = value { return string }
                   return nil
               }).first {
                return activityURLHost(query).map { "Reading \($0)" } ?? "Searching \"\(query)\""
            }
            return "Looking up"
        case "search_emails": return "Searching emails"
        case "read_emails": return "Reading emails"
        case "list_emails": return "Listing emails"
        case "list_folders": return "Listing email folders"
        case "draft_email": return "Drafting an email"
        case "generate_image": return "Generating an image"
        default:
            if case .string(let detail)? = arguments["detail"], let host = activityURLHost(detail) {
                return "Using \(name) · \(host)"
            }
            return "Using \(name)"
        }
    }
}

private func activityURLHost(_ string: String) -> String? {
    guard let host = URLComponents(string: string)?.host else { return nil }
    return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
}

private enum ActivityPreview {
    static func run(state: AgentProgressRunState) -> AgentProgressRunItem {
        AgentProgressRunItem(id: "preview", agent: "chat", scope: .foreground, steps: [
            AgentProgressStepItem(number: 1, maxSteps: 4, tools: [
                AgentProgressToolItem(name: "browse", arguments: ["queries": .array([.string("https://example.com")])]),
                AgentProgressToolItem(name: "search_emails")
            ], thinking: "I’ll check the source and compare it with the saved information.\nThen I can put the findings together."),
            AgentProgressStepItem(number: 2, maxSteps: 4,
                                  thinking: "The sources agree on the main points. I can now explain what I found.")
        ], state: state)
    }
}

#Preview("Working") {
    AgentActivityCard(runs: [ActivityPreview.run(state: .running)], isTyping: true)
        .padding()
}

#Preview("Completed · large text") {
    AgentActivityCard(runs: [ActivityPreview.run(state: .finished(.completed))], isTyping: false)
        .padding()
        .environment(\.dynamicTypeSize, .accessibility2)
}

#Preview("Interrupted") {
    AgentActivityCard(runs: [ActivityPreview.run(state: .interrupted)], isTyping: false)
        .padding()
}
