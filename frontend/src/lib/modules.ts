// The module vocabulary, and what each one is called in front of a person.
//
// `policy.Module` on the server is the source of truth, and a database CHECK
// keeps that honest. This is the frontend's copy, in one file rather than one
// per page: the permission grid and the API token editor both need it, and two
// copies drifting apart would mean a module you can grant but cannot scope, or
// the reverse — visible only by opening both files side by side.
//
// A key here that the server does not know renders as itself rather than
// vanishing, which is the failure worth having: a missing row in a permission
// grid reads as "not applicable", and that is a lie about access.

export const MODULES = [
  ['tasks', 'Tasks'],
  ['notes', 'Notes'],
  ['calendar', 'Calendar'],
  ['kitchen', 'Kitchen'],
  ['shopping', 'Shopping'],
  ['contacts', 'Contacts'],
  ['health', 'Health'],
  ['users', 'Members'],
  ['settings', 'Settings'],
  ['audit', 'Audit log'],
] as const

export type ModuleKey = (typeof MODULES)[number][0]

export function moduleLabel(key: string): string {
  return MODULES.find(([value]) => value === key)?.[1] ?? key
}
