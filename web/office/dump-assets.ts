// Decode pixel-agents' bundled PNG assets into the ServerMessages its own server sends on
// webviewReady, so a non-Node backend can replay them verbatim.
// Usage (from the pixel-agents checkout): npx tsx dump-assets.ts <assetsRoot> <out.json>
import * as fs from 'fs';

import {
  loadCarpetTiles,
  loadCharacterSprites,
  loadDefaultLayout,
  loadFloorTiles,
  loadFurnitureAssets,
  loadPetSprites,
  loadWallTiles,
} from './server/src/assetLoader.js';

async function main(): Promise<void> {
  const root = process.argv[2] ?? 'webview-ui/public';
  const out = process.argv[3] ?? 'office-boot.json';

  const [chars, pets, floors, walls, carpets, furniture] = await Promise.all([
    loadCharacterSprites(root),
    loadPetSprites(root),
    loadFloorTiles(root),
    loadWallTiles(root),
    loadCarpetTiles(root),
    loadFurnitureAssets(root),
  ]);

  const assets: Record<string, unknown>[] = [];
  if (chars) assets.push({ type: 'characterSpritesLoaded', characters: chars.characters });
  if (pets) {
    assets.push({
      type: 'petSpritesLoaded',
      pets: pets.pets,
      petNames: pets.manifests.map((m) => m.name),
    });
  }
  if (floors) assets.push({ type: 'floorTilesLoaded', sprites: floors.sprites });
  if (walls) assets.push({ type: 'wallTilesLoaded', sets: walls.sets });
  if (carpets) assets.push({ type: 'carpetTilesLoaded', sets: carpets.sets });
  if (furniture) {
    assets.push({
      type: 'furnitureAssetsLoaded',
      catalog: furniture.catalog,
      sprites: Object.fromEntries(furniture.sprites),
    });
  }
  const boot = {
    assets,
    defaultLayout: loadDefaultLayout(root),
    paletteCount: chars?.characters.length ?? 6,
  };
  fs.writeFileSync(out, JSON.stringify(boot));
  console.log(`wrote ${out}: ${assets.map((a) => a.type).join(', ')}`);
}

main().catch((err: unknown) => {
  console.error(err);
  process.exit(1);
});
