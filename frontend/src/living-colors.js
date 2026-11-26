// Theme-owned accents, not colors sampled from images. Contrast is checked against
// the strongest possible background tint to keep decorative headings legible.
const accents={
 neo:['#7950b5','#b35071','#326d9e','#ae592b'],
 avant:['#b22d20','#2445ac','#765021','#754e91'],
 candy:['#702bb5','#a53171','#24558e','#8b4921'],
 klein:['#f3f0e7','#ffd1b6','#bce0ff','#fbe7a2'],
 school:['#78452b','#9a382b','#344e9b','#5a466d'],
 riso:['#a8345b','#376b9e','#8e4825','#735193'],
 obsidian:['#e4e2dc','#c7b0e7','#a5c4e1','#e1b39f'],
 porcelain:['#55427e','#9b405e','#38648b','#985527'],
 lavender:['#68478c','#9c4068','#376994','#995627'],
 midnight:['#cad3ff','#d9b5ee','#9ccde8','#ffc4a6'],
 'cat-latte':['#8839ef','#d20f39','#1e66f5','#179299'],
 'cat-frappe':['#ca9ee6','#f4b8e4','#8caaee','#81c8be'],
 'cat-macchiato':['#c6a0f6','#f5bde6','#8aadf4','#8bd5ca'],
 'cat-mocha':['#cba6f7','#f5c2e7','#89b4fa','#94e2d5']
}
const rgb=h=>h.match(/[0-9a-f]{2}/gi).map(x=>parseInt(x,16))
const mix=(a,b,t)=>a.map((x,i)=>Math.round(x+(b[i]-x)*t))
const luminance=a=>a.map(x=>x/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((s,x,i)=>s+x*[.2126,.7152,.0722][i],0)
const ratio=(a,b)=>{const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}
export function livingColors(t){
 const colors=accents[t.id]||[t.accent,t.pop,t.link||t.accent,t.ink]
 const paper=rgb(t.paper),ink=rgb(t.ink),strength=t.dark?.13:.18
 const backgrounds=[paper,...[...colors,t.pop].map(c=>mix(paper,rgb(c),strength)),mix(paper,rgb(t.pop),strength*2)]
 const safe=c=>{const original=rgb(c);for(let i=0;i<=20;i++){const candidate=mix(original,ink,i/20);if(backgrounds.every(b=>ratio(candidate,b)>=4.5))return 'rgb('+candidate.join(',')+')'}return t.ink}
 return Object.fromEntries([...colors.map((c,i)=>['--living-title-'+i,safe(c)]),['--living-glow-0',colors[0]],['--living-glow-1',t.pop],['--living-glow-2',colors[2]],['--living-strength',String(strength)]])
}
