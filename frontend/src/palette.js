const cache=new Map()
export async function palette(src){
 if(!src||!src.startsWith('data:image/'))return null
 if(cache.has(src))return cache.get(src)
 const work=(async()=>{try{
 const image=new Image();image.src=src;await image.decode()
 const c=document.createElement('canvas');c.width=36;c.height=24;const ctx=c.getContext('2d',{willReadFrequently:true});ctx.drawImage(image,0,0,36,24)
 const pixels=ctx.getImageData(0,0,36,24).data;const buckets=new Map()
 for(let i=0;i<pixels.length;i+=4){const [r,g,b]=pixels.subarray(i,i+3);const max=Math.max(r,g,b),min=Math.min(r,g,b),sat=(max-min)/Math.max(1,max);if(max<38||max>246||sat<.12)continue;const key=[r,g,b].map(x=>Math.floor(x/40)).join(',');const v=buckets.get(key)||{n:0,r:0,g:0,b:0};v.n+=1+sat*.5;v.r+=r*(1+sat*.5);v.g+=g*(1+sat*.5);v.b+=b*(1+sat*.5);buckets.set(key,v)}
 const best=[...buckets.values()].sort((a,b)=>b.n-a.n)[0];if(!best)return null;const rgb=[best.r,best.g,best.b].map(x=>Math.round(x/best.n));const css=a=>'rgb('+a.join(' ')+')';const mix=(base,t)=>rgb.map((x,i)=>Math.round(x*t+base[i]*(1-t)));return {accent:css(mix([236,238,232],.43)),ink:css(mix([16,18,21],.19)),surface:css(mix([28,30,32],.19)),line:css(mix([74,76,78],.27)),light:css(mix([248,247,244],.095)),lightAccent:css(mix([25,28,31],.50))}
 }catch{return null}})();cache.set(src,work);return work
}
