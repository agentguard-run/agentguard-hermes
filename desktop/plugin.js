import { host, useValue, Popover, PopoverTrigger, PopoverContent } from '@hermes/plugin-sdk'
import { useState, useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const unavailable = { chip: 'AgentGuard unavailable', text: 'Enable AgentGuard in this gateway profile to read its status.' }

export function StatusChip({ ctx }) {
  const profile = useValue(host.state.profile)
  const connection = useValue(host.state.connectionId)
  const gateway = useValue(host.state.gateway)
  const scope = JSON.stringify([profile, connection, gateway])
  const [result, setResult] = useState(null)
  useEffect(() => {
    let active = true
    let pending = false
    const refresh = async () => {
      if (pending) return
      pending = true
      try {
        const data = await ctx.rest('/status')
        if (active) setResult({ scope, data: data && typeof data.chip === 'string' && typeof data.text === 'string' ? data : unavailable })
      } catch {
        if (active) setResult({ scope, data: unavailable })
      } finally {
        pending = false
      }
    }
    refresh()
    const cancel = ctx.setInterval(refresh, 30000)
    return () => { active = false; cancel() }
  }, [ctx, scope])
  const data = result?.scope === scope ? result.data : { chip: 'AgentGuard loading', text: 'Reading local spend and helper counters.' }
  return jsxs(Popover, {
    children: [
      jsx(PopoverTrigger, { asChild: true, children: jsx('button', {
        type: 'button', className: 'px-1.5 text-xs', 'aria-label': 'AgentGuard status', children: data.chip
      }) }),
      jsx(PopoverContent, { side: 'top', align: 'end', style: { width: 'min(32rem, 90vw)' },
        children: jsx('pre', { style: { whiteSpace: 'pre-wrap', font: 'inherit' }, children: data.text }) })
    ]
  })
}

export default {
  id: 'agentguard-spend',
  name: 'AgentGuard',
  register(ctx) {
    ctx.register({ id: 'status', area: 'statusBar.right', order: 130, render: () => jsx(StatusChip, { ctx }) })
  }
}
