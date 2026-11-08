// Hold a real report after Node's native stdio initialization, until the PTY controller has switched to raw.
import {connect} from 'node:net';

await new Promise((resolve, reject) => {
  const socket = connect(process.env.PAIR_REPORT_BARRIER);
  socket.setTimeout(10_000, () => socket.destroy(new Error('report barrier timed out')));
  socket.on('error', reject);
  socket.on('connect', () => socket.write(`${process.pid}\n`));
  socket.once('data', () => { socket.end(); resolve(); });
});
