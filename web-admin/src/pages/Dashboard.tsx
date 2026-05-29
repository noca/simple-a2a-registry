import React,{useEffect,useState,useCallback} from 'react';
import {Card,Row,Col,Statistic,Typography,Table,Spin,Space} from 'antd';
import {RobotOutlined,CheckCircleOutlined,WarningOutlined,ApiOutlined,ThunderboltOutlined,CloseCircleOutlined} from '@ant-design/icons';
import {getHealth,listAgents,getV2Stats,getAdminStats} from '../api/client';
import StatusTag from '../components/StatusTag';
const {Title}=Typography;
const Dashboard:React.FC=()=>{
  const [h,setH]=useState<any>(null);const [ag,setAg]=useState<any[]>([]);
  const [as,setAs]=useState<any>(null);const [vs,setVs]=useState<any>(null);const [lo,setLo]=useState(true);
  const fetch=useCallback(async()=>{
    setLo(true);
    const [a,b,c,d]=await Promise.allSettled([getHealth(),listAgents({limit:5}),getAdminStats(),getV2Stats()]);
    if(a.status==='fulfilled')setH(a.value);if(b.status==='fulfilled')setAg(b.value.agents||[]);
    if(c.status==='fulfilled')setAs(c.value);if(d.status==='fulfilled')setVs(d.value);setLo(false);
  },[]);
  useEffect(()=>{fetch();const t=setInterval(fetch,30000);return()=>clearInterval(t);},[fetch]);
  const sc=[
    {t:'Total Agents',v:as?.totalAgents??h?.stats?.total_agents??0,i:<RobotOutlined/>,c:'var(--text)'},
    {t:'Online',v:as?.aliveAgents??h?.stats?.alive_agents??0,i:<CheckCircleOutlined/>,c:'var(--green)'},
    {t:'Stale',v:as?.staleAgents??h?.stats?.stale_agents??0,i:<WarningOutlined/>,c:'var(--yellow)'},
    {t:'WebSocket',v:h?.stats?.connected_via_ws??0,i:<ApiOutlined/>,c:'var(--accent)'},
    {t:'Running',v:vs?.by_status?.running??0,i:<ThunderboltOutlined/>,c:'var(--purple)'},
    {t:'Blocked',v:vs?.by_status?.blocked??0,suffix:vs?.by_status?`/${Object.values(vs.by_status).reduce((a:number,b:any)=>a+(b||0),0)} tasks`:undefined,i:<CloseCircleOutlined/>,c:'var(--orange)'},
  ];
  const distItems=[{l:'Todo',c:vs?.by_status?.todo??0,cl:'#86868B'},{l:'Ready',c:vs?.by_status?.ready??0,cl:'#007AFF'},{l:'Running',c:vs?.by_status?.running??0,cl:'#BF5AF2'},{l:'Completed',c:vs?.by_status?.completed??0,cl:'#30D158'},{l:'Blocked',c:vs?.by_status?.blocked??0,cl:'#FF9F0A'},{l:'Failed',c:vs?.by_status?.failed??0,cl:'#FF453A'}];
  return React.createElement(Spin,{spinning:lo},
    React.createElement(Title,{level:3,style:{marginBottom:24,fontWeight:600,fontSize:22}},
      'Dashboard',h?.version&&React.createElement('span',{style:{fontSize:13,fontWeight:400,color:'var(--text-secondary)',marginLeft:12}},'v'+h.version)),
    React.createElement(Row,{gutter:[12,12],style:{marginBottom:24}},sc.map(s=>
      React.createElement(Col,{xs:12,sm:8,md:6,lg:4,key:s.t},
        React.createElement(Card,{hoverable:true,bodyStyle:{padding:'16px 20px'}},
          React.createElement(Statistic,{title:React.createElement('span',{style:{fontSize:11,color:'var(--text-secondary)',textTransform:'uppercase',letterSpacing:0.5}},s.t),value:s.v,suffix:s.suffix,valueStyle:{fontSize:28,fontWeight:700,color:s.c},prefix:React.cloneElement(s.i,{style:{fontSize:20,marginRight:4}})}))))),
    React.createElement(Row,{gutter:[16,16]},
      React.createElement(Col,{xs:24,lg:12},
        React.createElement(Card,{title:React.createElement('span',{style:{fontSize:13,fontWeight:600,color:'var(--text-secondary)',textTransform:'uppercase',letterSpacing:0.5}},'\u{1F916} Recent Agents')},
          React.createElement(Table,{dataSource:ag.slice(0,8),columns:[{title:'Name',dataIndex:'name',render:(n:any,r:any)=>React.createElement('span',{style:{fontWeight:500}},n||r.id?.substring(0,12))},{title:'Status',render:(_:any,r:any)=>r.connection==='websocket'?React.createElement(StatusTag,{status:'alive',pulse:true}):React.createElement(StatusTag,{status:r.status||'offline'})},{title:'URL',dataIndex:'url',render:(u:string)=>u?React.createElement('span',{style:{fontSize:12,color:'var(--text-secondary)'}},u):'-'}],rowKey:'id',pagination:false,size:'small',showHeader:false}))),
      React.createElement(Col,{xs:24,lg:12},
        React.createElement(Card,{title:React.createElement('span',{style:{fontSize:13,fontWeight:600,color:'var(--text-secondary)',textTransform:'uppercase',letterSpacing:0.5}},'\u{1F4CA} Status Distribution')},
          React.createElement(Space,{direction:'vertical',size:'small',style:{width:'100%',padding:'8px 0'}},distItems.map(i=>
            React.createElement('div',{key:i.l,style:{display:'flex',alignItems:'center',justifyContent:'space-between',padding:'6px 0'}},
              React.createElement('div',{style:{display:'flex',alignItems:'center',gap:8}},
                React.createElement('span',{style:{width:8,height:8,borderRadius:'50%',background:i.cl}}),
                React.createElement('span',{style:{fontSize:13}},i.l)),
              React.createElement('span',{style:{fontSize:18,fontWeight:700,color:i.cl}},i.c))))))));
};
export default Dashboard;
