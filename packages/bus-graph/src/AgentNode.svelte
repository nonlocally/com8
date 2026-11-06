<script>
  import { Handle, Position } from '@xyflow/svelte';
  import { deviceName, owner, communityColor } from './model.mjs';
  let { data, selected } = $props();
</script>

<div class="cg-agent" data-conductor={data.conductor ? "true" : undefined} data-flow-root={data.flowRoot ? "true" : undefined} style:--cg-community-color={data.community ? communityColor(data.community.colorIndex) : undefined} class:has-community={Boolean(data.community)} class:is-focused={data.focused} class:is-selected={selected} class:is-dimmed={data.dimmed} class:is-neighbor={data.neighbor}>
  <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
  <div class="cg-node-top"><span class="cg-kind">{data.agent.kind}{#if data.flowRoot}<span class="cg-conductor-badge">Flow root</span>{:else if data.conductor}<span class="cg-conductor-badge">Conductor</span>{/if}</span><span class="cg-node-status"><i class:available={data.agent.status === 'live' || data.agent.status === 'reachable'} class:queued={data.agent.status === 'queuable' || data.agent.status === 'queueable'}></i>{data.agent.status}</span></div>
  <div class="cg-node-name" title={data.agent.name}>{data.agent.name}</div>
  <div class="cg-node-owner">{owner(data.agent)}</div>
  {#if data.community || data.pinned}<div class="cg-node-tags">{#if data.community}<span class="cg-node-community">{data.community.label}</span>{/if}{#if data.pinned}<span class="cg-node-pin">Pinned</span>{/if}</div>{/if}
  {#if data.agent.via}<div class="cg-node-device" title={`run by ${data.agent.viaName}`}>via {data.agent.viaName}</div>{:else}<div class="cg-node-device" title={deviceName(data.agent)}>{deviceName(data.agent)}</div>{/if}
  <Handle type="source" position={Position.Right} id="out" isConnectable={false} />
  <Handle class="cg-direction-handle" type="target" position={Position.Right} id="in-right" isConnectable={false} />
  <Handle class="cg-direction-handle" type="source" position={Position.Left} id="out-left" isConnectable={false} />
  <Handle class="cg-direction-handle" type="target" position={Position.Top} id="in-top" isConnectable={false} />
  <Handle class="cg-direction-handle" type="source" position={Position.Top} id="out-top" isConnectable={false} />
  <Handle class="cg-direction-handle" type="target" position={Position.Bottom} id="in-bottom" isConnectable={false} />
  <Handle class="cg-direction-handle" type="source" position={Position.Bottom} id="out-bottom" isConnectable={false} />
</div>
