import {
  ButtonItem,
  DialogBody,
  DialogBodyText,
  DialogButtonPrimary,
  DialogButtonSecondary,
  DialogFooter,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { useCallback, useState } from "react";

import {
  calibrateStatus,
  getCapabilityDump,
  getStatusReport,
  probeMaxima,
  sendRaw,
  setProbe,
  takeSnapshot,
} from "./api";

export function DebugSection({
  connected,
  busy,
  onReopen,
  debug,
  onSetDebug,
}: {
  connected: boolean;
  busy: boolean;
  onReopen: () => void;
  debug: boolean;
  onSetDebug: (on: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [cmd, setCmd] = useState("00");
  const [value, setValue] = useState("00");
  const [out, setOut] = useState<string | null>(null);
  const [dump, setDump] = useState<{ title: string; body: string } | null>(null);
  const [dumping, setDumping] = useState<string | null>(null);
  const [probeOn, setProbeOn] = useState(false);

  const runDump = useCallback(
    async (
      title: string,
      fn: () => Promise<{ dump?: string; error?: string }>,
    ) => {
      setDumping(title);
      try {
        const r = await fn();
        setDump({ title, body: r.dump ?? r.error ?? "(空)" });
      } catch (e) {
        setDump({ title, body: String(e) });
      } finally {
        setDumping(null);
      }
    },
    [],
  );

  const onRaw = useCallback(async () => {
    try {
      const n = Number.parseInt(cmd, 16);
      const v = Number.parseInt(value, 16);
      if (Number.isNaN(n) || Number.isNaN(v)) {
        setOut("十六进制格式无效 / invalid hex");
        return;
      }
      const r = await sendRaw(n, v, []);
      setOut(JSON.stringify(r, null, 2));
    } catch (e) {
      setOut(String(e));
    }
  }, [cmd, value]);

  return (
    <PanelSection title="调试 Debug">
      <PanelSectionRow>
        {/* The switch itself is always here: it is the only way back to the
            tools it hides, so hiding it with them would strand the panel in
            whichever state it was left in. */}
        <ToggleField
          label="调试模式"
          description="Debug mode — 显示调试工具并记录详细日志"
          checked={debug}
          onChange={onSetDebug}
        />
      </PanelSectionRow>
      {!debug ? (
        <PanelSectionRow>
          <div style={{ opacity: 0.6, fontSize: "0.85em", lineHeight: 1.4 }}>
            调试工具与逐帧日志已隐藏。排查问题时打开上面的开关，日志里会重新
            出现每一帧的收发和启动时的协议表。
          </div>
        </PanelSectionRow>
      ) : (
        <>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={!connected} onClick={() => setOpen(true)}>
          发送原始指令 Raw command
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || dumping !== null}
          onClick={() =>
            void runDump("能力位 Capability bitmap (0xE0)", getCapabilityDump)
          }
        >
          {dumping ? "读取中…" : "读取能力位 Capability bitmap (0xE0)"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || dumping !== null}
          onClick={() => void runDump("音量上限探测 Volume probe", probeMaxima)}
        >
          {dumping ? "探测中…" : "探测音量上限（会短暂改变音量）"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || dumping !== null}
          onClick={() => void runDump("标定 0xE3 偏移 Calibrate offsets", calibrateStatus)}
        >
          {dumping ? "标定中…" : "标定 0xE3 偏移（会依次改动并还原设置）"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || dumping !== null}
          onClick={() => void runDump("状态快照 Snapshot (0x00 + 0xE3)", takeSnapshot)}
        >
          {dumping ? "读取中…" : "抓取状态快照（对比用）"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || dumping !== null}
          onClick={() => void runDump("状态报告 Status report (0xE3)", getStatusReport)}
        >
          {dumping ? "读取中…" : "读取状态报告 Status report (0xE3)"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={onReopen}>
          {busy ? "重连中…" : "重连 USB（面板无响应时用）"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="响应差分 Response diffing"
          checked={probeOn}
          description="每次改动前后各读一轮设备并记录变化的字节。用于查找新偏移量,但会让每次设置多打 4 个 USB 事务,这个端点会超时 —— 默认关闭。"
          disabled={!connected}
          onChange={(v: boolean) => {
            setProbeOn(v);
            void setProbe(v).catch((e) => {
              setProbeOn(!v);
              throw e;
            });
          }}
        />
      </PanelSectionRow>
        </>
      )}

      {dump && (
        <PanelSectionRow>
          <pre
            style={{
              fontSize: "0.7em",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              maxHeight: 260,
              overflow: "auto",
              width: "100%",
            }}
          >
            {`${dump.title}\n${dump.body}`}
          </pre>
        </PanelSectionRow>
      )}

      {open && (
        <ModalRoot
          onCancel={() => setOpen(false)}
          closeModal={() => setOpen(false)}
          title="发送原始指令 Raw command"
        >
          <DialogBody>
            <DialogBodyText>
              直接向眼镜发送一帧原始指令。仅用于排查,值请填十六进制。
            </DialogBodyText>
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <input
                value={cmd}
                onChange={(e) => setCmd(e.target.value)}
                placeholder="cmd"
                style={{ flex: 1 }}
              />
              <input
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder="value"
                style={{ flex: 1 }}
              />
            </div>
            {out && (
              <pre style={{ fontSize: "0.7em", whiteSpace: "pre-wrap" }}>
                {out}
              </pre>
            )}
          </DialogBody>
          <DialogFooter>
            <DialogButtonPrimary onClick={() => void onRaw()}>
              发送 Send
            </DialogButtonPrimary>
            <DialogButtonSecondary onClick={() => setOpen(false)}>
              关闭 Close
            </DialogButtonSecondary>
          </DialogFooter>
        </ModalRoot>
      )}
    </PanelSection>
  );
}
