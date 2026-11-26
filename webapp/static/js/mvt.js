// Minimal Mapbox Vector Tile (spec 2.1) decoder. No dependencies; works in the browser and in Node.
const utf8 = new TextDecoder();

class Reader {
  constructor(bytes) { this.b = bytes; this.p = 0; }
  get done() { return this.p >= this.b.length; }
  varint() { // multiplication, not shifts, so values above 32 bits stay correct
    let v = 0, mul = 1, byte;
    do {
      if (this.p >= this.b.length) throw new Error('truncated vector tile');
      byte = this.b[this.p++];
      v += (byte & 0x7f) * mul;
      mul *= 128;
    } while (byte & 0x80);
    return v;
  }
  bytes() { const n = this.varint(); const out = this.b.subarray(this.p, this.p + n); this.p += n; return out; }
  fixed(n, get) { const v = new DataView(this.b.buffer, this.b.byteOffset + this.p, n)[get](0, true); this.p += n; return v; }
  skip(wire) {
    if (wire === 0) this.varint();
    else if (wire === 1) this.p += 8;
    else if (wire === 2) this.bytes();
    else if (wire === 5) this.p += 4;
    else throw new Error('unsupported wire type');
  }
  each(fn) { while (!this.done) { const key = this.varint(); if (!fn(Math.floor(key / 8), key & 7)) this.skip(key & 7); } }
}
const zigzag = (n) => (n % 2 ? -(n + 1) / 2 : n / 2);

function packed(bytes) {
  const r = new Reader(bytes), out = [];
  while (!r.done) out.push(r.varint());
  return out;
}

function value(bytes) {
  const r = new Reader(bytes);
  let v = null;
  r.each((f) => {
    if (f === 1) v = utf8.decode(r.bytes());
    else if (f === 2) v = r.fixed(4, 'getFloat32');
    else if (f === 3) v = r.fixed(8, 'getFloat64');
    else if (f === 4 || f === 5) v = r.varint();
    else if (f === 6) v = zigzag(r.varint());
    else if (f === 7) v = !!r.varint();
    else return false;
    return true;
  });
  return v;
}

/** Geometry commands → array of paths, each a flat [x0, y0, x1, y1, …] in tile units. */
function geometry(cmds) {
  const paths = [];
  let x = 0, y = 0, path = null, i = 0;
  while (i < cmds.length) {
    const cmd = cmds[i] & 7, count = cmds[i] >> 3;
    i++;
    if (cmd === 7) { if (path) path.push(path[0], path[1]); continue; } // ClosePath
    for (let k = 0; k < count; k++) {
      x += zigzag(cmds[i++]); y += zigzag(cmds[i++]);
      if (cmd === 1) { path = [x, y]; paths.push(path); } else path.push(x, y);
    }
  }
  return paths;
}

/** ArrayBuffer | Uint8Array → { layerName: { extent, features: [{ type: 1|2|3, props, paths }] } } */
export function decodeTile(data) {
  const layers = {};
  const root = new Reader(data instanceof Uint8Array ? data : new Uint8Array(data));
  root.each((field) => {
    if (field !== 3) return false;
    const r = new Reader(root.bytes());
    const keys = [], values = [], raw = [];
    let name = '', extent = 4096;
    r.each((f) => {
      if (f === 1) name = utf8.decode(r.bytes());
      else if (f === 2) raw.push(r.bytes());
      else if (f === 3) keys.push(utf8.decode(r.bytes()));
      else if (f === 4) values.push(value(r.bytes()));
      else if (f === 5) extent = r.varint();
      else return false;
      return true;
    });
    layers[name] = {
      extent,
      features: raw.map((bytes) => {
        const fr = new Reader(bytes);
        let tags = [], type = 0, cmds = [];
        fr.each((f) => {
          if (f === 2) tags = packed(fr.bytes());
          else if (f === 3) type = fr.varint();
          else if (f === 4) cmds = packed(fr.bytes());
          else return false;
          return true;
        });
        const props = {};
        for (let i = 0; i + 1 < tags.length; i += 2) props[keys[tags[i]]] = values[tags[i + 1]];
        return { type, props, paths: geometry(cmds) };
      }),
    };
    return true;
  });
  return layers;
}
