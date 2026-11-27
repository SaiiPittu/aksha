// Client-side checks, before a request is sent. The server checks again.

export const MIN_PASSWORD = 8
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export type Errors<K extends string> = Partial<Record<K, string>>

export const emailError = (v: string) =>
  !v.trim() ? "Enter your email." : EMAIL.test(v.trim()) ? undefined : "Enter a valid email address."

export const newPasswordError = (v: string) =>
  v.length >= MIN_PASSWORD ? undefined : `Use at least ${MIN_PASSWORD} characters.`

export const requiredError = (v: string, what: string) => (v.trim() ? undefined : `Enter ${what}.`)

export const hasErrors = (e: Record<string, string | undefined>) => Object.values(e).some(Boolean)
