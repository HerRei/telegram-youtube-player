const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const first = {
  token: "first-token",
  kind: "video",
  media_id: "first",
  start_seconds: 12
};
const second = {
  token: "second-token",
  kind: "video",
  media_id: "second",
  start_seconds: 0
};
let state = {current: first, pending: [second]};
const requests = [];
const loads = [];
let events = null;

global.window = global;
global.setInterval = () => 0;
global.fetch = async (path, options = {}) => {
  requests.push({path, options});
  if (path === "/api/advance") {
    assert.strictEqual(JSON.parse(options.body).token, first.token);
    state = {current: second, pending: []};
  }
  return {ok: true, status: 200, json: async () => state};
};

const mockPlayer = {
  loadVideoById: item => loads.push(item.videoId),
  loadPlaylist: item => loads.push(item.list),
  stopVideo: () => loads.push("stopped"),
  getPlaylist: () => [],
  getPlaylistIndex: () => -1
};
global.YT = {
  PlayerState: {ENDED: 0, PLAYING: 1},
  Player: function (_element, options) {
    events = options.events;
    return mockPlayer;
  }
};

vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));

const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  await flush();
  window.onYouTubeIframeAPIReady();
  events.onReady();
  assert.deepStrictEqual(loads, ["first"]);
  events.onStateChange({data: YT.PlayerState.PLAYING});
  events.onStateChange({data: YT.PlayerState.ENDED});
  await flush();
  await flush();
  assert.deepStrictEqual(loads, ["first", "second"]);
  assert.strictEqual(requests.filter(request => request.path === "/api/advance").length, 1);
  process.stdout.write("player queue script: ok\n");
})().catch(error => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
