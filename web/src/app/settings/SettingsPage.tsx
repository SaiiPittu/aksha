import { useViewer, canManageOrg } from "@/features/auth/principal"
import { SettingsLayout, type SettingsKey } from "@/app/pages/common"

/** Every settings screen sits in the settings sub-nav. */
export function SettingsFrame({ active, children }: { active: SettingsKey; children: React.ReactNode }) {
  const viewer = useViewer()
  return <SettingsLayout active={active} org={viewer.org} manager={canManageOrg(viewer.role)}>{children}</SettingsLayout>
}
