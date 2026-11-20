import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import StatusBadge from '../src/components/StatusBadge.vue'
import OverviewView from '../src/views/OverviewView.vue'
import RunsView from '../src/views/RunsView.vue'

vi.mock('../src/api', async () => {
  const actual = await vi.importActual('../src/api')
  return {
    ...actual,
    api: vi.fn(async (path) => {
      if (path === '/overview') {
        return {
          run_total: 3,
          run_needs_review: 1,
          run_failed: 0,
          ledger_published_amount: 22125.99,
          ledger_published_count: 18,
          adjustment_pending_count: 2,
          adjustment_pending_delta: 120.5,
          adjustment_approved_delta: 0,
          recent_runs: [
            { id: 1, run_code: 'RUN-1', strategy: 'initial', status: 'success', target_windows: 18, computed_windows: 18, reused_windows: 0, node_count: 72, cutoff_at: '2025-09-01T00:00:00Z', published_at: '2025-09-01T01:00:00Z' },
          ],
          ledger_by_period: [{ period_month: '2025-08', amount: 22125.99, entry_count: 18 }],
          strategy_stats: [{ strategy: 'targeted', runs: 1, avg_duration_ms: 800, computed_windows: 1, reused_windows: 17 }],
        }
      }
      if (path === '/runs') return [
        { id: 1, run_code: 'RUN-1', strategy: 'initial', status: 'success', version: 1, target_windows: 18, computed_windows: 18, reused_windows: 0, node_count: 72, duration_ms: 800, cutoff_at: '2025-09-01T00:00:00Z', published_at: null, window_counts: { success: 18 } },
        { id: 2, run_code: 'RUN-2', strategy: 'full_period', status: 'needs_review', version: 2, target_windows: 18, computed_windows: 18, reused_windows: 0, node_count: 72, duration_ms: 900, cutoff_at: '2025-09-02T00:00:00Z', published_at: null, window_counts: { needs_review: 18 } },
      ]
      if (path === '/events') return []
      throw new Error(`unexpected path ${path}`)
    }),
  }
})

beforeEach(() => {
  globalThis.fetch = vi.fn()
})

describe('StatusBadge', () => {
  it('maps status to Chinese label and class', () => {
    const ok = mount(StatusBadge, { props: { status: 'success' } })
    expect(ok.text()).toBe('成功')
    expect(ok.classes()).toContain('ok')
    const missing = mount(StatusBadge, { props: { status: 'needs_review' } })
    expect(missing.text()).toBe('待核')
    expect(missing.classes()).toContain('warn')
    const blocked = mount(StatusBadge, { props: { status: 'blocked' } })
    expect(blocked.classes()).toContain('danger')
  })
})

describe('OverviewView', () => {
  it('renders published total and pending adjustment delta', async () => {
    const wrapper = mount(OverviewView, { global: { stubs: { RouterLink: true } } })
    await new Promise((r) => setTimeout(r, 0))
    const text = wrapper.text()
    expect(text).toContain('已发布账本合计')
    expect(text).toContain('22,125.99')
    expect(text).toContain('120.50')
    expect(text).toContain('RUN-1')
  })
})

describe('RunsView', () => {
  it('shows run list with statuses and disables publish for non-success', async () => {
    const wrapper = mount(RunsView, { global: { stubs: { RouterLink: true } } })
    await new Promise((r) => setTimeout(r, 0))
    const text = wrapper.text()
    expect(text).toContain('RUN-1')
    expect(text).toContain('RUN-2')
    expect(text).toContain('整期重跑')
    expect(text).toContain('待核')
  })
})
