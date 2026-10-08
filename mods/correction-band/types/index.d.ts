declare module 'claude-code' {
  interface PluginState {
    'correction-band': {
      correction: { text: string; pattern: string; phrase: string; at: number } | null
      recall: {
        candidates: number
        lastSuccess: string | null
        stale: boolean
        error: string | null
        reviewedAt: number | null
      } | null
    }
  }
}
