import { useState } from "react";
import { truncateProductName } from "../labels";

interface Props {
  name: string;
  className?: string;
}

/** A product name, truncated to ~60 characters. Hovering shows the full name
 * (native `title` tooltip); tapping/clicking toggles it in place, since
 * touch devices have no hover. */
export function ProductName({ name, className }: Props) {
  const [expanded, setExpanded] = useState(false);
  const truncated = truncateProductName(name);

  if (truncated === name) {
    return <span className={className}>{name}</span>;
  }

  return (
    <button
      type="button"
      className={className ? `${className} product-name-toggle` : "product-name-toggle"}
      title={name}
      onClick={() => setExpanded((e) => !e)}
    >
      {expanded ? name : truncated}
    </button>
  );
}
