import SwiftUI
import PennyDesign
import PennyServices

struct ImageAttachmentSettingsSection: View {
    let viewModel: SettingsViewModel

    var body: some View {
        Section {
            ForEach(viewModel.visibleImageSettings) { setting in
                Toggle(isOn: Binding(
                    get: { viewModel.imageSettingValue(setting) },
                    set: { viewModel.setImageSetting(setting, enabled: $0) }
                )) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(setting.title)
                        if let detail = setting.detail {
                            Text(detail)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .accessibilityIdentifier(AXID.imageAttachmentSetting(setting.rawValue))
                .disabled(!viewModel.canEditImageSetting(setting))
            }
            if let status = viewModel.imageSettingsStatus {
                Text(status)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let error = viewModel.client.imageAttachmentSettings.error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                Button("Refresh Settings") { viewModel.refresh() }
                    .disabled(!viewModel.client.canSend)
            }
        } header: {
            HStack(spacing: 8) {
                Text("Image Attachments")
                if viewModel.client.imageAttachmentSettings.pendingSetting != nil {
                    ProgressView()
                        .controlSize(.mini)
                        .accessibilityLabel("Saving image setting")
                }
            }
        }
    }
}

extension SettingsViewModel {
    var visibleImageSettings: [ImageAttachmentSetting] {
        ImageAttachmentSetting.allCases.filter { setting in
            !setting.requiresServerConfiguration || client.isImageAttachmentSettingAvailable(setting)
        }
    }

    var imageSettingsStatus: String? {
        if !client.canSend { return "Connect to edit shared image settings." }
        if !client.hasLoadedRuntimeConfig { return "Loading image settings…" }
        if !client.supportsImageAttachmentSettings { return "Update the server to enable image settings." }
        return nil
    }

    func imageSettingValue(_ setting: ImageAttachmentSetting) -> Bool {
        if client.imageAttachmentSettings.pendingSetting == setting,
           let pendingValue = client.imageAttachmentSettings.pendingValue {
            return pendingValue
        }
        guard let value = client.runtimeConfigParams.first(where: { $0.key == setting.rawValue })?.value else {
            return !setting.requiresServerConfiguration
        }
        return value == "1"
    }

    func canEditImageSetting(_ setting: ImageAttachmentSetting) -> Bool {
        client.canSend && client.supportsImageAttachmentSettings
            && client.isImageAttachmentSettingAvailable(setting)
            && client.imageAttachmentSettings.pendingSetting == nil
            && (setting == .automatic || setting == .scheduledReportEditing || imageSettingValue(.automatic))
    }

    func setImageSetting(_ setting: ImageAttachmentSetting, enabled: Bool) {
        guard canEditImageSetting(setting), enabled != imageSettingValue(setting) else { return }
        client.updateImageAttachmentSetting(setting, enabled: enabled)
    }
}

private extension ImageAttachmentSetting {
    var title: String {
        switch self {
        case .automatic: "Automatic images"
        case .citedPage: "Images from cited pages"
        case .sameSite: "Images from the same site"
        case .related: "Related images"
        case .scheduledReportEditing: "Edit scheduled report images"
        }
    }

    var detail: String? {
        switch self {
        case .related: "Includes previously generated images."
        case .scheduledReportEditing: "Uses ComfyUI to edit images selected for collection reports."
        default: nil
        }
    }
}
