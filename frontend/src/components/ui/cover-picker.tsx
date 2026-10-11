// "Cover colour" dialog: the notebook's book shown large in the chosen colour, with a swatch for each cloth.
import { useState } from "react";
import { COVERS, coverByKey } from "@/lib/covers";
import { modal } from "../overlays";
import { Book } from "./book";

export function pickCover(title: string, current: string): Promise<string | undefined> {
  return modal<string>((close) => <CoverPicker title={title} current={current} close={close} />);
}

function CoverPicker({ title, current, close }: { title: string; current: string; close: (value?: string) => void }) {
  const [chosen, setChosen] = useState(current);
  return (
    <>
      <h2>Cover colour</h2>
      <p>How “{title}” looks on your shelf.</p>
      <div className="cover-preview"><Book title={title} cover={coverByKey(chosen)} width={112} /></div>
      <div className="swatches" role="radiogroup" aria-label="Cover colour">
        {COVERS.map((c) => (
          <button key={c.key} className={`swatch${c.key === chosen ? " on" : ""}`} role="radio" aria-checked={c.key === chosen}
            aria-label={c.name} data-tip={c.name} style={{ background: c.bg }} onClick={() => setChosen(c.key)} />
        ))}
      </div>
      <div className="actions">
        <button className="btn" onClick={() => close()}>Cancel</button>
        <button className="btn primary" disabled={chosen === current} onClick={() => close(chosen)}>Save</button>
      </div>
    </>
  );
}
