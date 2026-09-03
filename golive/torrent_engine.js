/**
 * Lightweight Node.js helper for streaming torrents via WebTorrent/torrent-stream.
 * Exposes a local HTTP server on 127.0.0.1:<port> for FFmpeg to consume.
 */

const http = require('http');
const path = require('path');

const magnetUri = process.argv[2];
const port = parseInt(process.argv[3] || '8890', 10);

if (!magnetUri) {
  console.error('Usage: node torrent_engine.js <magnet_uri> [port]');
  process.exit(1);
}

let WebTorrent;
try {
  WebTorrent = require('webtorrent');
} catch (err) {
  console.error('WebTorrent module not found:', err.message);
  process.exit(1);
}

const client = new WebTorrent();

client.on('error', (err) => {
  console.error('Torrent client error:', err.message);
});

client.add(magnetUri, (torrent) => {
  console.log(`[TORRENT] Metadata loaded: "${torrent.name}" (${torrent.files.length} files)`);

  // Identify largest video file (.mkv, .mp4, .avi, .mov, .webm)
  let videoFile = torrent.files.find(f => /\.(mkv|mp4|avi|mov|webm|m4v)$/i.test(f.name));
  if (!videoFile && torrent.files.length > 0) {
    videoFile = torrent.files.reduce((prev, curr) => (prev.length > curr.length ? prev : curr));
  }

  if (!videoFile) {
    console.error('[TORRENT] No suitable video file found in torrent');
    process.exit(1);
  }

  const fileIdx = torrent.files.indexOf(videoFile);
  console.log(`[TORRENT] Selected file [${fileIdx}]: "${videoFile.name}" (${(videoFile.length / 1024 / 1024).toFixed(1)} MB)`);

  // Create HTTP server for torrent files
  const server = torrent.createServer();
  server.listen(port, '127.0.0.1', () => {
    const streamUrl = `http://127.0.0.1:${port}/${fileIdx}`;
    console.log(`TORRENT_ENGINE_READY ${streamUrl}`);
  });
});
