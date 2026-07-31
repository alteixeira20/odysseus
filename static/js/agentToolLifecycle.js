export function stopToolNodeTimers(node) {
  if (!node) return;
  if (node._waveInterval) {
    clearInterval(node._waveInterval);
    node._waveInterval = null;
  }
  if (node._elapsedTicker) {
    clearInterval(node._elapsedTicker);
    node._elapsedTicker = null;
  }
}

export function toolTerminalState(event) {
  if (event?.timed_out) return 'timed out';
  if (event?.cancelled) return 'cancelled';
  return (event?.exit_code === 0 || event?.exit_code == null) ? 'done' : 'failed';
}

export function runTerminalToolState(state) {
  if (state === 'completed' || state === 'done') return 'done';
  if (state === 'cancelled' || state === 'stopped') return 'cancelled';
  if (state === 'error' || state === 'failed') return 'failed';
  return 'interrupted';
}

export function settleToolNode(node, state) {
  if (!node || !node.classList.contains('running')) return false;
  stopToolNodeTimers(node);
  node.classList.remove('running');
  if (state && state !== 'done') node.classList.add('error');

  const wave = node.querySelector('.agent-thread-wave');
  if (wave) wave.remove();
  const icon = node.querySelector('.agent-thread-icon');
  if (icon) {
    icon.textContent = state === 'done'
      ? '\u2713'
      : (state === 'cancelled' ? '\u25A0' : '\u2717');
  }
  let status = node.querySelector('.agent-thread-status');
  if (!status && node.ownerDocument) {
    status = node.ownerDocument.createElement('span');
    status.className = 'agent-thread-status';
    node.querySelector('.agent-thread-header')?.appendChild(status);
  }
  if (status) status.textContent = state || 'interrupted';
  return true;
}

export function settleRunningToolNodes(root, state) {
  if (!root?.querySelectorAll) return 0;
  let settled = 0;
  root.querySelectorAll('.agent-thread-node.running').forEach((node) => {
    if (settleToolNode(node, state)) settled += 1;
  });
  return settled;
}
