# Frontend

Everything the browser runs lives in one file: `frontend/index.html`
(4,911 lines — HTML, CSS, vanilla JS and three.js scene code). Two vendored
libraries are served alongside it.

---

## 1. File map

| Path | Lines | What it is |
| --- | ---: | --- |
| `frontend/index.html` | 4,911 | The whole application |
| `frontend/vendor/three.min.js` | — | three.js r147, minified |
| `frontend/vendor/OrbitControls.js` | 730 | Orbit / pan / zoom |

three.js is vendored rather than loaded from a CDN so the app renders with no
internet connection. `ensure()` falls back to jsDelivr only if the vendored
files are missing.

---

## 2. The document, top to bottom

```
<style>            design tokens + every stylesheet rule
<body>
  SVG sprite       16 icon symbols, referenced by <use href="#i-map">
  .atmo            background atmosphere (grid, glow, haze)
  #dome-entrance   metallic split-dome intro
  #login-screen    sign in / sign up
  #app             the application shell
    .navbar        brand + section tabs + role badge + exit
    #rolebar       access-level banner, shown after sign-in
    .mobile-nav    bottom nav for small screens
    #section-home      / .section-registry  / .section-users
    #section-cases     / .section-analytics / .section-officers
    #section-criminals / .section-map
  modals           FIR form, criminal form, case detail, criminal dossier
<script src=vendor/…>
<script>           all application logic
```

The SVG sprite is why there is no emoji anywhere: every icon is an outlined
path that inherits `currentColor`, so it matches the surrounding text at any
size and in any colour.

---

## 3. Design tokens

All theming runs through CSS variables declared once on `:root`.

```css
--p3:#38e1ff     primary cyan      --bg:#050a13   page background
--p6:#1c3a63     deep blue         --gb:rgba(…)   panel glass
--acc:#38e1ff    accent            --gbr:rgba(…)  border
--crit:#ff6a5e   critical          --gbrh:rgba(…) border, hovered
--f-d: 'Rajdhani'    headings
--f-m: 'Share Tech Mono'  data, labels
--f-x: 'Michroma'      brand display
```

Semantic colours are fixed deliberately: **critical and high are always warm**,
whatever the theme. An operator should never have to learn that "red means
serious" only on some screens.

The `.metal` surface class is the shared panel treatment — brushed gradient,
fine directional grain, a specular top edge and corner brackets. Cards, modals,
the home hero and the list rows all use it, which is what makes a card on the
cases page and a card on the home page read as the same object.

---

## 4. JavaScript modules

The script is one scope. These are the logical modules inside it.

### 4.1 State

```js
let OL=[], CRL=[], CASES=[], ST=[], OM={}, CRM={}, RELATIONS=[];
let USERS=[], REG={total:0,byState:[],users:[]};
let CU=null;                    // current user
let activeSec='home';
```

`OM` and `CRM` are id→record maps for O(1) lookup; the arrays keep order.
`fetchInitialData()` fills all of them from six parallel `fetch` calls.

### 4.2 Session and permissions

```js
function can(action){
  return ({
    public: ['view'],
    police: ['view','add','edit'],
    admin:  ['view','add','edit','delete'],
  }[CU?.role] || []).includes(action);
}
```

Mirrors the server's `_require()`. Used **only** to decide which buttons to
render — the server refuses regardless.

`apiWrite(method, url, body)` attaches `Authorization: Bearer <token>`; the
token comes from `login`, is kept in `localStorage.csn_token`, and is cleared on
exit.

### 4.3 Section router

```js
function showSec(n){ … }
```

Hides the other seven sections, syncs desktop and mobile tabs, redirects
`users` to Home unless the role is admin and `registry` unless police or admin,
calls the matching renderer, then resizes every mounted WebGL world after an
80 ms delay so the layout has settled.

The redirect is defence in depth — the tab is already hidden — but it means
`showSec('users')` in the console does nothing for a police account.

### 4.4 The three.js engine — `CSN3D`

The largest subsystem. Everything visual is wrapped in `World(container, opts)`.

```js
World(el, {stage, autoRotate, onPick, …})
  ├─ scene, camera, renderer, OrbitControls
  ├─ grid, lights, star field, state polygons
  ├─ setStations() / setPins() / addArc()   ← the command map
  ├─ clear(), onFrame(), pick(), view(), focus(), focusArea()
  └─ dispose()
```

`pushStage(kind, elementId, builder, payload)` keeps one world per kind and
feeds it new payloads rather than rebuilding. `w.onFrame(fn)` registers a
callback that runs inside the single shared `requestAnimationFrame` loop — so
five simultaneous scenes still cost one loop.

`dispose()` matters: the view switches rebuild instanced meshes constantly, and
without explicit disposal of geometries, materials and textures WebGL contexts
leak until the tab dies.

### 4.5 Renderers

| Function | Page | What it draws |
| --- | --- | --- |
| `renderHome()` | Home | Destination cards + live totals |
| `renderRegistry()` | Public Registry | Police/admin view of citizen accounts |
| `renderUsers()` | Users | Admin view of every account |
| `renderCaseRegister()` | Cases | Filterable, groupable FIR list |
| `renderOfficers()` | Officers | Roster with home-state filter |
| `renderCriminals()` | Criminals | State-grouped list + wheel payload |
| `initAnalytics()` | Analytics | Six-quadrant volume payload |
| `openCP()` / `openOP()` / `openCase()` | — | The three dossiers |
| `openEditForm()` / `openEditUser()` | — | Admin edit dialogs |

---

## 5. Page-by-page

### Home
Live totals (stations, FIRs, officers, tracked) and a card per destination.
The "Register FIR" card only renders when `can('add')`.

### Command map
The whole of India as extruded state polygons. The eight South India states are
lifted, lit, outlined, labelled, and drawn with corner posts; every other state
stays muted slate. 6,006 station pins become instanced pillars; 200 tracked
suspects float above their handling station. Clicking a pillar loads its FIR
intelligence into the side panel.

### Cases
KPIs reflect the whole register, not the filtered slice, so the headline numbers
do not jump as you filter. Rows carry a completion bar; clicking opens a dossier
with 20 fields and click-through links to the criminal, officer and station.

### Officers
A sortable, filterable list. Each officer is assigned a **home state** — the one
holding most of their caseload — and filtering by state returns that state's own
officers rather than everyone who ever opened a case there. Secondary coverage
is listed as "Also covers".

### Criminals
City clusters on a rotating wheel, joined by shared officer / station / city /
modus operandi. Every link kind renders as two batched ribbons plus a 1 px core
line, and a travelling pulse rides along it. Click a node for the dossier: mugshot,
16-field personal record, 3D connection web, known links with reasons, station
mini-map, full FIR history.

### Public Registry
Visible to police and Govt Officials. **Only** `role='public'` rows are ever
returned by the server; phone numbers are stripped server-side. Four KPI tiles
give the count, states covered, how many can self-serve a reset by email, and how
many can only be reached by phone.

### Users
Admin only. Every account with role, clearance tier, post, station and an explicit
`RIGHTS:` line. Edit covers display name, access level and the whole profile;
delete is guarded so the last admin cannot be removed or demoted.

---

## 6. The two permission UIs

`rowActions(kind, id, label)` builds the Edit/Delete cluster for a list row, and
`bindRowActions(scope, kind)` wires them by delegation:

```js
if(can('edit'))   …'<button data-act="edit">Edit</button>'
if(can('delete')) …'<button data-act="del">Delete</button>'
```

A Public account therefore sees a clean list with no buttons at all. A police
officer sees Edit only. An admin sees both.

---

## 7. Rendering performance notes

| Technique | Where | Why |
| --- | --- | --- |
| `InstancedMesh` | station pillars, network nodes, officer figures | one draw call for hundreds of objects |
| Merged geometry | link ribbons, glow bands | 854 links → ~12 draw calls |
| One rAF loop | `CSN3D` | five scenes, one loop |
| World reuse | `pushStage` | tab switches never rebuild WebGL |
| Sprite labels | all text in 3D | 200+ canvas textures would be fatal, so they are built on demand and cached |

One subtle trap worth recording: `material.vertexColors = true` on an
`InstancedMesh` whose geometry has **no** `color` attribute renders the object
black. Instance colours arrive through `instanceColor`, which three.js applies
independently. Setting `vertexColors` additionally makes the shader expect a
per-vertex attribute that primitives do not have.

---

## 8. Accessibility and responsiveness

- Every interactive control is a real `<button>` or `<select>`, keyboard
  reachable.
- Below 900 px the desktop tabs collapse into a bottom nav and the map side
  panel becomes a slide-up sheet.
- Focus rings and contrast meet WCAG AA on the metallic palette.
- **Gap:** the 3D canvases have no keyboard equivalent. The wheel and the map
  are mouse/touch only; the list views are the accessible route to the same data.

---

## 9. Known frontend issues

- `index.html` is one 4,911-line file. Splitting it into ES modules is the
  single biggest maintainability win available and needs no build step — the
  browser already understands `<script type="module">`.
- The station payload is ~1.4 MB of JSON on every cold start.
- Emoji removed from the icon set, but a few remain in the seed data's `remarks`
  column, which is user content rather than markup.